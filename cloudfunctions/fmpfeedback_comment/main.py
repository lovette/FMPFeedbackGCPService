"""FMPFeedbackGCPService Google Cloud HTTP Cloud Function `fmpfeedback_comment`

This handler is triggered by a HTTP request.
The handler stores feedback submission to Firestore and publishes a Pub/Sub message to announce feedback document creation.
Any files attached to the comment will be uploaded via `fmpfeedback_upload` prior to the comment being posted.
The "uploads token" ties the uploads and comment together as one submission.

For project details, see:
https://github.com/lovette/FMPFeedbackGCPService
"""

__author__ = "Lance Lovette"
__copyright__ = "Copyright (c) 2021 Lance Lovette"
__license__ = "MIT"


from datetime import datetime, timezone
from flask import Request, abort
from google.cloud import firestore, pubsub
from typing import Any
import google
import json
import os


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

# Token shared between client and cloud functions to authenticate requests.
# This variable is required for functions to operate.
FEEDBACK_SENDER_AUTHTOKEN = os.getenv("FEEDBACK_SENDER_AUTHTOKEN")
if not FEEDBACK_SENDER_AUTHTOKEN:
    print("ERROR! FEEDBACK_SENDER_AUTHTOKEN must be defined as a runtime environment variable.")

FEEDBACK_MAX_PENDING_SUBMITS = 5  # Seems reasonable; shared with fmpfeedback_upload
FEEDBACK_FIRESTORE_COLLECTION = os.getenv("FEEDBACK_FIRESTORE_COLLECTION", "fmpfeedback")

FEEDBACK_PUBSUB_TOPIC = os.getenv("FEEDBACK_PUBSUB_TOPIC", "fmpfeedback")
FEEDBACK_PUBSUB_FIELD_ACTION = "feedbackAction"
FEEDBACK_PUBSUB_FIELD_DOCID = "feedbackDocId"
FEEDBACK_PUBSUB_ACTION_NEWFEEDBACK = "feedbackSumitted"

FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP = "archivedTimestamp"
FEEDBACKDOC_FIELD_CLIENTIP = "clientIp"
FEEDBACKDOC_FIELD_CREATETIMESTAMP = "feedbackTimestamp"
FEEDBACKDOC_FIELD_EMAIL = "feedbackEmail"
FEEDBACKDOC_FIELD_HASUPLOADS = "hasUploads"
FEEDBACKDOC_FIELD_MESSAGE = "feedbackMessage"
FEEDBACKDOC_FIELD_NAME = "feedbackName"
FEEDBACKDOC_FIELD_SUBJECT = "feedbackSubject"

# Empty feedback document; should match definition in fmpfeedback_upload
FEEDBACK_EMPTY_DOC = {
    FEEDBACKDOC_FIELD_CLIENTIP: "",
    FEEDBACKDOC_FIELD_CREATETIMESTAMP: "",
    FEEDBACKDOC_FIELD_EMAIL: "",
    FEEDBACKDOC_FIELD_HASUPLOADS: False,
    FEEDBACKDOC_FIELD_MESSAGE: "",
    FEEDBACKDOC_FIELD_NAME: "",
    FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP: "",
    FEEDBACKDOC_FIELD_SUBJECT: "",
}


#####################################################################
# Cloud function entrypoint

def fmpfeedback_comment(request: Request) -> Any:
    """Cloud Function HTTP Entrypoint

    Args:
        request (Request): Framework request data

    Returns:
        "OK" if feedback was accepted or an error message with HTTP 4xx status code.
        (Technically, client ignores response content.)
        Anything other than status 2xx will cause client to notify user of failure.
    """

    def _abort_return(client_error: str, internal_error: str = None) -> tuple:
        if internal_error:
            print(f"ERROR! Feedback submit failed: {internal_error}")
        else:
            print(f"ERROR! Feedback submit failed: {client_error}")
        return client_error, 400

    if request.method == "GET":
        # Feedback must be submitted via POST but as a convenience we can at least
        # sanity check the authtoken to help get started.
        if not FEEDBACK_SENDER_AUTHTOKEN:
            err, _ = _abort_return("You must define FEEDBACK_SENDER_AUTHTOKEN as a Runtime Environment Variable. See README for details.")
            return err

        abort(405)  # method not allowed

    auth_username = request.authorization.username  # foo@bar.com/token
    auth_token = request.authorization.password     # FEEDBACK_SENDER_AUTHTOKEN

    if request.content_type != "application/json":
        return _abort_return("BAD CONTENT")

    if not auth_token:
        return _abort_return("BAD TOKEN")
    if auth_token != FEEDBACK_SENDER_AUTHTOKEN:
        return _abort_return("BAD TOKEN")

    try:
        feedback_json = request.json["request"]
        feedback_email = feedback_json["requester"]["email"]
        feedback_subject = feedback_json["subject"]
        feedback_body = feedback_json["comment"]["body"]
    except KeyError:
        return _abort_return("BAD DATA")

    if not feedback_email:
        return _abort_return("BAD DATA")
    if not feedback_subject:
        return _abort_return("BAD DATA")
    if not feedback_body:
        return _abort_return("BAD DATA")

    if not auth_username:
        return _abort_return("BAD AUTH")
    if auth_username != f"{feedback_email}/token":
        return _abort_return("BAD AUTH")

    feedback_doc = {
        FEEDBACKDOC_FIELD_SUBJECT: feedback_subject,
        FEEDBACKDOC_FIELD_MESSAGE: feedback_body,
    }

    feedback_name = feedback_json["requester"].get("name")  # optional
    if feedback_name:
        feedback_doc[FEEDBACKDOC_FIELD_NAME] = feedback_name

    # An "uploads token" will be included if any files were attached to the feedback submission,
    # in which case it will reference a stub document we need to update with feedback details.
    fs_feedback_doc_id = feedback_json["comment"].get("uploads")
    if fs_feedback_doc_id:
        fs_feedback_doc_id = next(iter(fs_feedback_doc_id))

    if fs_feedback_doc_id:
        print(f"Received feedback from: {feedback_email}; uploads stored with feedback {fs_feedback_doc_id}")
    else:
        print(f"Received feedback from: {feedback_email}; no uploads")

    # 1. Store feedback document in Firestore collection

    try:
        fs_client = firestore.Client()

        fs_feedback_coll = fs_client.collection(FEEDBACK_FIRESTORE_COLLECTION)

        if fs_feedback_doc_id:
            # Update details for existing feedback document with attachments
            fs_feedback_doc = fs_feedback_coll.document(fs_feedback_doc_id)
            fs_feedback_doc.update(feedback_doc)
        else:
            fs_feedback_docs = fs_feedback_coll.where(FEEDBACKDOC_FIELD_EMAIL, "==", feedback_email).where(
                FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP, "==", "").get()

            # Prevent submitting too much feedback
            if len(fs_feedback_docs) >= FEEDBACK_MAX_PENDING_SUBMITS:
                return _abort_return("TOO MUCH FEEDBACK", f"TOO MUCH FEEDBACK FROM {feedback_email}")

            feedback_doc.update({
                FEEDBACKDOC_FIELD_EMAIL: feedback_email,
                FEEDBACKDOC_FIELD_CREATETIMESTAMP: datetime.now(timezone.utc).isoformat(timespec="seconds"),
                FEEDBACKDOC_FIELD_CLIENTIP: request.headers.get("X-Forwarded-For", request.remote_addr),
            })

            # Store feedback document
            _, fs_feedback_doc = fs_feedback_coll.add(FEEDBACK_EMPTY_DOC | feedback_doc)
            fs_feedback_doc_id = fs_feedback_doc.id

    except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Feedback Firestore operation failed: Firestore auth exception: {e}")
    except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
        return _abort_return("FIRESTORE FAIL", f"Feedback Firestore operation failed: Firestore client exception: {e}")
    except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Feedback Firestore operation failed: Firestore API exception: {e}")
    except Exception as e:
        return _abort_return("FIRESTORE FAIL", f"Feedback Firestore operation failed: Unexpected exception: {e}")

    # 2. Publish Pub/Sub message to notify subscribers a feedback document was submitted

    try:
        ps_client = pubsub.PublisherClient()

        topic_path = ps_client.topic_path(fs_client.project, FEEDBACK_PUBSUB_TOPIC)

        try:
            # Topic should have been created before Cloud Functions execute
            topic = ps_client.get_topic(topic=topic_path)
        except google.api_core.exceptions.NotFound:
            return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} failed: Topic does not exist: {topic_path}")

        ps_message = json.dumps({
            FEEDBACK_PUBSUB_FIELD_ACTION: FEEDBACK_PUBSUB_ACTION_NEWFEEDBACK,
            FEEDBACK_PUBSUB_FIELD_DOCID: fs_feedback_doc_id,
        }, separators=(',', ':'))

        # Block until publish is complete, raise exception on error
        ps_future = ps_client.publish(topic.name, ps_message.encode())
        ps_event_id = ps_future.result()

    except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
        return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Pub/Sub auth exception: {e}")
    except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
        return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Pub/Sub client exception: {e}")
    except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
        return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Pub/Sub API exception: {e}")
    except Exception as e:
        return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Unexpected exception: {e}")
    else:
        print(f"Published {FEEDBACK_PUBSUB_FIELD_ACTION} '{FEEDBACK_PUBSUB_ACTION_NEWFEEDBACK}' to Pub/Sub topic: {ps_event_id}")

    return "OK"
