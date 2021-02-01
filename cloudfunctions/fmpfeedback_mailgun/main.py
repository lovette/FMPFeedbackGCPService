"""FMPFeedbackGCPService Google Cloud Pub/Sub Cloud Function `fmpfeedback_mailgun_pubsub`

This handler is triggered when a Pub/Sub message is published to the `fmpfeedback` topic
announcing that a feedback document has been submitted and saved to Firestore.
Each feedback request is forwarded via email mesage through Mailgun ESP REST API.
Messages are published by `fmpfeedback_comment` and `fmpfeedback_caretaker`.

For project details, see:
https://github.com/lovette/FMPFeedbackGCPService
"""

__author__ = "Lance Lovette"
__copyright__ = "Copyright (c) 2021 Lance Lovette"
__license__ = "MIT"


from datetime import datetime
from flask import Request
from google.cloud import firestore
from google.cloud.firestore_v1.base_document import DocumentSnapshot  # for type annotation
from typing import Any
import base64
import email
import google
import json
import mimetypes
import os
import requests
import sys

#####################################################################
# Load runtime environment variables from .env

running_as_cloud_function = os.getenv("FUNCTION_TARGET") is not None

# If running as Cloud Function attempt to load environment variables from .env file.
# (Flask does this automatically when run local.)
if running_as_cloud_function:
    from dotenv import load_dotenv
    load_dotenv()


#####################################################################
# Constants

FEEDBACK_FIRESTORE_COLLECTION = os.getenv("FEEDBACK_FIRESTORE_COLLECTION", "fmpfeedback")
FEEDBACK_UPLOADS_SUBCOLLECTION = "uploads"

FEEDBACK_PUBSUB_TOPIC = os.getenv("FEEDBACK_PUBSUB_TOPIC", "fmpfeedback")
FEEDBACK_PUBSUB_FIELD_ACTION = "feedbackAction"
FEEDBACK_PUBSUB_FIELD_DOCID = "feedbackDocId"
FEEDBACK_PUBSUB_ACTION_NEWFEEDBACK = "feedbackSumitted"
FEEDBACK_PUBSUB_ACTION_CARETAKER_RETRY = "caretakerRetry"

FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP = "archivedTimestamp"
FEEDBACKDOC_FIELD_CLIENTIP = "clientIp"
FEEDBACKDOC_FIELD_CREATETIMESTAMP = "feedbackTimestamp"
FEEDBACKDOC_FIELD_EMAIL = "feedbackEmail"
FEEDBACKDOC_FIELD_HASUPLOADS = "hasUploads"
FEEDBACKDOC_FIELD_MESSAGE = "feedbackMessage"
FEEDBACKDOC_FIELD_NAME = "feedbackName"
FEEDBACKDOC_FIELD_SUBJECT = "feedbackSubject"

UPLOADDOC_FIELD_CONTENTLENGTH = "contentLength"
UPLOADDOC_FIELD_DATA = "data"
UPLOADDOC_FIELD_FILENAME = "filename"
UPLOADDOC_FIELD_UPLOADIGNORED = "uploadIgnored"

# Field particular to this handler
FEEDBACKDOC_FIELD_MAILGUN_MESSAGEID = "mailgunMessageId"


#####################################################################
# Mailgun constants

MAILGUN_API_DOMAIN = None
MAILGUN_API_KEY = None
MAILGUN_SENDER = None
MAILGUN_RECIPIENT = None
MAILGUN_RUNTIME_VARS_MISSING = False

# Define constants required for function to operate.
module = sys.modules[__name__]
for name in ("MAILGUN_API_DOMAIN", "MAILGUN_API_KEY", "MAILGUN_SENDER", "MAILGUN_RECIPIENT"):
    value = os.getenv(name, "")
    setattr(module, name, value)
    if not value:
        print(f"ERROR! {name} must be defined as a runtime environment variable.")
        MAILGUN_RUNTIME_VARS_MISSING = True

MAILGUN_REQUESTS_URL = f"https://api.mailgun.net/v3/{MAILGUN_API_DOMAIN}/messages"
MAILGUN_SENDER = email.utils.formataddr(email.utils.parseaddr(MAILGUN_SENDER))
MAILGUN_SENDER_ADDR = email.utils.parseaddr(MAILGUN_SENDER)[1]
MAILGUN_RECIPIENT = email.utils.formataddr(email.utils.parseaddr(MAILGUN_RECIPIENT))
MAILGUN_API_AUTHUSER = "api"


#####################################################################
# Cloud function entrypoint

def fmpfeedback_mailgun_pubsub(event: dict, context) -> None:
    """Cloud Function Pub/Sub Entrypoint

    Invoked when a Pub/Sub message is published to the `fmpfeedback` topic
    announcing that a feedback document has been submitted and saved to Firestore.
    Feedback request is forwarded via email mesage through Mailgun ESP REST API.

    Args:
        event (dict): Event properties
        context (google.cloud.functions.Context): Event context

    Returns:
        Nothing
        If any exceptions are raised, the message will be automatically NACKED and retried.
    """

    ps_event_id = context.event_id

    print(f"Received Pub/Sub message: {ps_event_id}")

    if MAILGUN_RUNTIME_VARS_MISSING:
        print("Pub/Sub message ignored: One or more MAILGUN_* runtime environment variables are not defined.")
        return

    def _abort_return(internal_error: str = None) -> None:
        if internal_error:
            print(f"ERROR! Forward feedback failed: {internal_error}")

    # Expected pubsub_message is JSON:
    # {
    #   FEEDBACK_PUBSUB_FIELD_ACTION: FEEDBACK_PUBSUB_ACTION_*,
    #   FEEDBACK_PUBSUB_FIELD_DOCID: fs_feedback_doc_id,
    # }
    pubsub_message = json.loads(base64.b64decode(event["data"]).decode("utf-8"))

    feedback_action = pubsub_message.get(FEEDBACK_PUBSUB_FIELD_ACTION, None)
    if not feedback_action:
        return _abort_return(f"Pub/Sub message ignored: Missing field {FEEDBACK_PUBSUB_FIELD_ACTION}")

    if feedback_action not in (FEEDBACK_PUBSUB_ACTION_NEWFEEDBACK, FEEDBACK_PUBSUB_ACTION_CARETAKER_RETRY):
        return _abort_return(f"Pub/Sub message ignored: '{feedback_action}' not intended for us")

    fs_feedback_doc_id = pubsub_message.get(FEEDBACK_PUBSUB_FIELD_DOCID, None)
    if not fs_feedback_doc_id:
        return _abort_return(f"Pub/Sub message ignored: '{feedback_action}' is missing field {FEEDBACK_PUBSUB_FIELD_DOCID}")

    try:
        fs_feedback_doc = firestore.Client().collection(FEEDBACK_FIRESTORE_COLLECTION).document(fs_feedback_doc_id).get()
    except google.api_core.exceptions.NotFound:
        return _abort_return(f"Pub/Sub action feedback document not found: {fs_feedback_doc_id}")
    except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
        return _abort_return(f"Feedback document abandoned: Firestore auth exception: {e}")
    except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
        return _abort_return(f"Feedback document abandoned: Firestore client exception: {e}")
    except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
        return _abort_return(f"Feedback document abandoned: Firestore API exception: {e}")
    except Exception as e:
        return _abort_return(f"Feedback document abandoned: Unexpected exception: {e}")
    else:
        if not _fmpfeedback_mailgun_send(fs_feedback_doc):
            print("Message not sent")
            # Ack for now, message will be retried by caretaker

    # Message is acked automatically upon successful function invocation.


#####################################################################
#

def fmpfeedback_mailgun_debug(request: Request) -> Any:
    """Cloud Function HTTP Entrypoint

    HTTP convenience function to facilitate debugging.
    Fetches any feedback documents from Firestore that have not been processed and archived
    and forwards each as an email mesage Mailgun ESP REST API.

    Args:
        request (Request): Framework request data

    Returns:
        "OK" if function runs to completion or an error message with HTTP 4xx status code.
    """

    def _abort_return(client_error: str, internal_error: str = None) -> tuple:
        if internal_error:
            print(f"ERROR! Forward feedback failed: {internal_error}")
        else:
            print(f"ERROR! Forward feedback failed: {client_error}")
        return client_error, 400

    if MAILGUN_RUNTIME_VARS_MISSING:
        return _abort_return("CONFIG FAIL", "ERROR! One or more MAILGUN_* runtime environment variables are not defined.")

    try:
        fs_feedback_docs = firestore.Client().collection(FEEDBACK_FIRESTORE_COLLECTION).where(
            FEEDBACKDOC_FIELD_MESSAGE, "!=", "").where(FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP, "==", "").get()
    except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Firestore auth exception: {e}")
    except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
        return _abort_return("FIRESTORE FAIL", f"Firestore client exception: {e}")
    except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Firestore API exception: {e}")
    except Exception as e:
        return _abort_return("FIRESTORE FAIL", f"Unexpected exception: {e}")
    else:
        for fs_doc in fs_feedback_docs:
            if not _fmpfeedback_mailgun_send(fs_doc):
                print("Message not sent")

    return "OK"


#####################################################################
# Internal helper

def _fmpfeedback_mailgun_send(fs_feedback_doc: DocumentSnapshot) -> bool:
    """Forward feedback via email message through Mailgun ESP REST API

    Args:
        fs_feedback_doc (DocumentSnapshot): Feedback document

    Returns:
        bool: True if email is accepted for delivery.
    """

    feedback_doc = fs_feedback_doc.to_dict()

    def _abort_return(internal_error: str = None) -> bool:
        if internal_error:
            print(f"ERROR! Forward feedback failed: {internal_error}")
        return False

    if feedback_doc[FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP]:
        print(f"Ignoring feedback document {fs_feedback_doc.id}: Feedback has already been archived")
        return True

    for field in (FEEDBACKDOC_FIELD_EMAIL, FEEDBACKDOC_FIELD_SUBJECT, FEEDBACKDOC_FIELD_MESSAGE):
        if not feedback_doc[field]:
            _abort_return(f"Ignoring feedback document {fs_feedback_doc.id}: Field '{field}' value is not set")

    attachments = []

    if feedback_doc[FEEDBACKDOC_FIELD_HASUPLOADS]:
        try:
            fs_upload_docs = fs_feedback_doc.reference.collection(FEEDBACK_UPLOADS_SUBCOLLECTION).get()
        except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
            _abort_return(f"Firestore auth exception: {e}")
        except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
            _abort_return(f"Firestore client exception: {e}")
        except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
            _abort_return(f"Firestore API exception: {e}")
        except Exception as e:
            _abort_return(f"Unexpected exception: {e}")
        else:
            for fs_upload_doc in fs_upload_docs:
                upload_doc = fs_upload_doc.to_dict()
                filename = upload_doc[UPLOADDOC_FIELD_FILENAME]
                data = upload_doc[UPLOADDOC_FIELD_DATA]
                mime_type, _ = mimetypes.guess_type(filename, strict=False)
                attachments.append(("attachment", (filename, data, mime_type or "")))

    from_name = feedback_doc[FEEDBACKDOC_FIELD_NAME] or False
    from_email = feedback_doc[FEEDBACKDOC_FIELD_EMAIL]
    reply_to = email.utils.formataddr((from_name, from_email))
    sender = email.utils.formataddr((f"{reply_to} via", MAILGUN_SENDER_ADDR))  # set "realname" to requester for clearer MUA presentation

    message_data = {
        "from": sender,
        "to": MAILGUN_RECIPIENT,
        "subject": feedback_doc[FEEDBACKDOC_FIELD_SUBJECT],
        "text": feedback_doc[FEEDBACKDOC_FIELD_MESSAGE],
        "h:sender": sender,  # prevent some MUA from showing "on behalf of"
        'h:reply-to': reply_to,  # allow "reply all" to include requester
        'h:X-Origin-Mailer': "FMPFeedbackGCPService.fmpfeedback_mailgun",
    }

    print(f"Forwarding feedback {fs_feedback_doc.id}: from '{reply_to}' to '{MAILGUN_SENDER_ADDR}' with {len(attachments)} attachments")

    try:
        response = requests.post(
            MAILGUN_REQUESTS_URL,
            auth=(MAILGUN_API_AUTHUSER, MAILGUN_API_KEY),
            files=attachments,
            data=message_data)

        response.raise_for_status()

    except requests.exceptions.HTTPError as e:
        _abort_return(f"Mailgun API HTTP exception: {e}")
    except requests.exceptions.RequestException as e:
        _abort_return(f"Mailgun API request exception: {e}")
    except Exception as e:
        _abort_return(f"Mailgun API unexpected exception: {e}")
    else:
        response_json = response.json()
        message_id = response_json["id"][1:-1]  # "<id>"

        print(f"Mailgun message accepted: message-id {message_id}")

        feedback_doc.update({
            FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP: datetime.utcnow().isoformat(),
            FEEDBACKDOC_FIELD_MAILGUN_MESSAGEID: message_id,
        })

        # Tag message as being dealt with
        fs_feedback_doc.reference.update(feedback_doc)

    return True
