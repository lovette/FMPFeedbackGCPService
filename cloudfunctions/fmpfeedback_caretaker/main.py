"""FMPFeedbackGCPService Google Cloud HTTP Cloud Function `fmpfeedback_caretaker`

This handler is triggered by a HTTP request and should be invoked on a regular schedule
(ideally daily) to perform housekeeping tasks on the Firestore feedback document collection.

Tasks including:
1. Delete archived feedback documents that have expired
2. Delete feedback documents that have uploads but no comment that occur if the client
   fails to invoke `fmpfeedback_comment` after `fmpfeedback_upload`.
3. Reprocess feedback documents that look to have been missed

For project details, see:
https://github.com/lovette/FMPFeedbackGCPService
"""

__author__ = "Lance Lovette"
__copyright__ = "Copyright (c) 2021 Lance Lovette"
__license__ = "MIT"


from datetime import datetime, timedelta
from flask import Request
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

CARETAKER_REPUBLISH_AFTER = int(os.getenv("CARETAKER_REPUBLISH_AFTER", 24))  # hours
CARETAKER_KEEP_HISTORY = int(os.getenv("CARETAKER_KEEP_HISTORY", 30))  # days


#####################################################################
# Cloud function entrypoint

def fmpfeedback_caretaker(request: Request) -> Any:
    """Cloud Function HTTP Entrypoint

    Args:
        request (Request): Framework request data

    Returns:
        "OK" if function runs to completion or an error message with HTTP 4xx status code.
    """

    def _abort_return(client_error: str, internal_error: str = None) -> tuple:
        if internal_error:
            print(f"ERROR! Caretaker task failed: {internal_error}")
        else:
            print(f"ERROR! Caretaker task failed: {client_error}")
        return client_error, 400

    stale_doc_ids = []
    republish_prior_to_date = datetime.utcnow() - timedelta(hours=CARETAKER_REPUBLISH_AFTER)
    delete_prior_to_date = datetime.utcnow() - timedelta(days=CARETAKER_KEEP_HISTORY)
    five_min_ago = datetime.utcnow() - timedelta(minutes=5)

    try:
        fs_client = firestore.Client()

        # Delete archived feedback documents that have expired
        print("Caretaker looking for archived feedback to expire...")

        fs_query = fs_client.collection(FEEDBACK_FIRESTORE_COLLECTION).where(FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP, "!=", "")
        for fs_feedback_doc in fs_query.stream():
            feedback_doc = fs_feedback_doc.to_dict()

            archived_timestamp = datetime.fromisoformat(feedback_doc[FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP])

            if archived_timestamp <= delete_prior_to_date:
                print(f"Deleting archived feedback document {fs_feedback_doc.id}, archived {archived_timestamp}")
                fs_feedback_doc.reference.delete()

        # Delete feedback documents that have uploads but no comment
        # (This would occur if the client failed to invoke fmpfeedback_comment after fmpfeedback_upload)
        print("Caretaker looking for feedback missing a message body...")

        fs_query = fs_client.collection(FEEDBACK_FIRESTORE_COLLECTION).where(FEEDBACKDOC_FIELD_MESSAGE, "==", "")
        for fs_feedback_doc in fs_query.stream():
            feedback_doc = fs_feedback_doc.to_dict()

            create_timestamp = datetime.fromisoformat(feedback_doc[FEEDBACKDOC_FIELD_CREATETIMESTAMP])

            if create_timestamp <= five_min_ago:
                print(f"Deleting feedback document {fs_feedback_doc.id} with no message body, created {create_timestamp}")
                fs_feedback_doc.reference.delete()

        # Find feedback documents that look to have been missed
        print("Caretaker looking for stale feedback...")

        fs_query = fs_client.collection(FEEDBACK_FIRESTORE_COLLECTION).where(FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP, "==", "")
        for fs_feedback_doc in fs_query.stream():
            feedback_doc = fs_feedback_doc.to_dict()

            create_timestamp = datetime.fromisoformat(feedback_doc[FEEDBACKDOC_FIELD_CREATETIMESTAMP])

            if create_timestamp <= republish_prior_to_date:
                print(f"Feedback document {fs_feedback_doc.id} is stale, created {create_timestamp}")
                stale_doc_ids.append(fs_feedback_doc.id)

    except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Firestore auth exception: {e}")
    except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
        return _abort_return("FIRESTORE FAIL", f"Firestore client exception: {e}")
    except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Firestore API exception: {e}")
    except Exception as e:
        return _abort_return("FIRESTORE FAIL", f"Unexpected exception: {e}")

    # Publish Pub/Sub messages to notify subscribers a feedback document is stale

    for fs_feedback_doc_id in stale_doc_ids:
        try:
            ps_client = pubsub.PublisherClient()

            topic_path = ps_client.topic_path(fs_client.project, FEEDBACK_PUBSUB_TOPIC)

            try:
                # Topic should have been created before Cloud Functions execute
                topic = ps_client.get_topic(topic=topic_path)
            except google.api_core.exceptions.NotFound:
                return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} failed: Topic does not exist: {topic_path}")

            ps_message = json.dumps({
                FEEDBACK_PUBSUB_FIELD_ACTION: FEEDBACK_PUBSUB_ACTION_CARETAKER_RETRY,
                FEEDBACK_PUBSUB_FIELD_DOCID: fs_feedback_doc_id,
            }, separators=(',', ':'))

            # Block until publish is complete, raise exception on error
            ps_future = ps_client.publish(topic.name, ps_message.encode())
            ps_event_id = ps_future.result()

            print(f"Published {FEEDBACK_PUBSUB_FIELD_ACTION} '{FEEDBACK_PUBSUB_ACTION_CARETAKER_RETRY}' to Pub/Sub topic: {ps_event_id}")

        except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
            return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Pub/Sub auth exception: {e}")
        except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
            return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Pub/Sub client exception: {e}")
        except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
            return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Pub/Sub API exception: {e}")
        except Exception as e:
            return _abort_return("PUBSUB FAIL", f"Feedback Pub/Sub {FEEDBACK_PUBSUB_FIELD_ACTION} publish failed: Unexpected exception: {e}")

    return "OK"
