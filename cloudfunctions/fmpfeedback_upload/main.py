"""FMPFeedbackGCPService Google Cloud HTTP Cloud Function `fmpfeedback_upload`

This handler is triggered by a HTTP request.
The handler stores an upload associated with feedback submission to Firestore and returns a "token".
The first upload will be an "anonymous system profile" if the user opted to include it.
Remaining uploads will follow.
Feedback comment properties will be posted to `fmpfeedback_comment` after all uploads are complete.
The "uploads token" ties the uploads and comment together as one submission.

For project details, see:
https://github.com/lovette/FMPFeedbackGCPService
"""

__author__ = "Lance Lovette"
__copyright__ = "Copyright (c) 2021 Lance Lovette"
__license__ = "MIT"


from datetime import datetime, timezone
from flask import jsonify
from flask import Request, abort
from google.cloud import firestore
from typing import Any
import google
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

FEEDBACK_FIRESTORE_COLLECTION = os.getenv("FEEDBACK_FIRESTORE_COLLECTION", "fmpfeedback")
FEEDBACK_UPLOADS_SUBCOLLECTION = "uploads"
FEEDBACK_MAX_PENDING_SUBMITS = 5  # Seems reasonable; shared with fmpfeedback_comment
FEEDBACK_MAX_UPLOADS = 10  # Hardcoded in FMPFeedbackGCPServiceSender.initWithDomain
FEEDBACK_MAX_UPLOAD_SIZE = 1 * 1024 * 1024  # MiB, Hardcoded in FMPFeedbackGCPServiceSender.initWithDomain

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

# Empty feedback document; should match definition in fmpfeedback_comment
FEEDBACK_EMPTY_DOC = {
    FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP: "",
    FEEDBACKDOC_FIELD_CLIENTIP: "",
    FEEDBACKDOC_FIELD_CREATETIMESTAMP: "",
    FEEDBACKDOC_FIELD_EMAIL: "",
    FEEDBACKDOC_FIELD_HASUPLOADS: False,
    FEEDBACKDOC_FIELD_MESSAGE: "",
    FEEDBACKDOC_FIELD_NAME: "",
    FEEDBACKDOC_FIELD_SUBJECT: "",
}


#####################################################################
# Cloud function entrypoint

def fmpfeedback_upload(request: Request) -> Any:
    """Cloud Function HTTP Entrypoint

    Args:
        request (Request): Framework request data

    Returns:
        JSON payload containing upload token or an error message with HTTP 4xx status code.
        Anything other than status 2xx will cause client to notify user of failure.
    """

    def _abort_return(client_error: str, internal_error: str = None) -> tuple:
        if internal_error:
            print(f"ERROR! Upload submit failed: {internal_error}")
        else:
            print(f"ERROR! Upload submit failed: {client_error}")
        return client_error, 400

    if request.method == "GET":
        # Uploads must be submitted via POST but as a convenience we can at least
        # sanity check the authtoken to help get started.
        if not FEEDBACK_SENDER_AUTHTOKEN:
            err, _ = _abort_return("You must define FEEDBACK_SENDER_AUTHTOKEN as a Runtime Environment Variable. See README for details.")
            return err

        abort(405)  # method not allowed

    auth_username = request.authorization.username  # foo@bar.com/token
    auth_token = request.authorization.password     # FEEDBACK_SENDER_AUTHTOKEN
    feedback_email = auth_username.removesuffix("/token")

    if not auth_username:
        return _abort_return("BAD AUTH")
    if feedback_email == auth_username:
        return _abort_return("BAD AUTH")

    if not auth_token:
        return _abort_return("BAD TOKEN")
    if auth_token != FEEDBACK_SENDER_AUTHTOKEN:
        return _abort_return("BAD TOKEN")

    try:
        upload_filename = request.args["filename"]
    except KeyError:
        return _abort_return("BAD FILENAME")

    if not upload_filename:
        return _abort_return("BAD FILENAME")

    if request.content_type != "application/binary":
        return _abort_return("BAD CONTENT")
    if not request.data:
        return _abort_return("BAD DATA")
    if len(request.data) > FEEDBACK_MAX_UPLOAD_SIZE:
        return _abort_return("BAD DATA")

    # An upload token will be included if any attachments were previously uploaded for feedback.
    fs_feedback_doc_id = request.args.get("token")

    print(f"Received upload from: {feedback_email}: '{upload_filename}': feedback document:{fs_feedback_doc_id}")

    upload_doc = {
        UPLOADDOC_FIELD_FILENAME: upload_filename,
        UPLOADDOC_FIELD_DATA: request.data,
    }

    try:
        fs_client = firestore.Client()

        fs_feedback_coll = fs_client.collection(FEEDBACK_FIRESTORE_COLLECTION)

        if fs_feedback_doc_id:
            feedback_doc = fs_feedback_coll.document(fs_feedback_doc_id)
            fs_uploads_coll = feedback_doc.collection(FEEDBACK_UPLOADS_SUBCOLLECTION)
            fs_upload_docs = fs_uploads_coll.get()

            # Prevent uploading too many attachments to a single feedback document.
            if len(fs_upload_docs) >= FEEDBACK_MAX_UPLOADS:
                # Make note of upload but don't raise an exception so  fmpfeedback_comment is still invoked.
                print(f"TOO MANY UPLOADS FROM {feedback_email}")
                upload_doc[UPLOADDOC_FIELD_DATA] = f"This upload was ignored; upload limit is {FEEDBACK_MAX_UPLOADS}"
                upload_doc[UPLOADDOC_FIELD_UPLOADIGNORED] = True
        else:
            # Prevent submitting too much feedback
            fs_feedback_docs = fs_feedback_coll.where(FEEDBACKDOC_FIELD_EMAIL, "==", feedback_email).where(
                FEEDBACKDOC_FIELD_ARCHIVEDTIMESTAMP, "==", "").get()

            if len(fs_feedback_docs) >= FEEDBACK_MAX_PENDING_SUBMITS:
                return _abort_return("TOO MUCH FEEDBACK", f"TOO MUCH FEEDBACK FROM {feedback_email}")

            # Stub feedback document, remaining feedback details will be set by fmpfeedback_comment
            feedback_doc = {
                FEEDBACKDOC_FIELD_EMAIL: feedback_email,
                FEEDBACKDOC_FIELD_CREATETIMESTAMP: datetime.now(timezone.utc).isoformat(timespec="seconds"),
                FEEDBACKDOC_FIELD_CLIENTIP: request.headers.get("X-Forwarded-For", request.remote_addr),
                FEEDBACKDOC_FIELD_HASUPLOADS: True,
            }

            # Add stub feedback document
            _, fs_feedback_doc = fs_feedback_coll.add(FEEDBACK_EMPTY_DOC | feedback_doc)

            fs_feedback_doc_id = fs_feedback_doc.id
            fs_uploads_coll = fs_feedback_doc.collection(FEEDBACK_UPLOADS_SUBCOLLECTION)

        upload_doc[UPLOADDOC_FIELD_CONTENTLENGTH] = len(upload_doc[UPLOADDOC_FIELD_DATA])

        # Add upload to existing feedback document
        fs_uploads_coll.add(upload_doc)

    except google.auth.exceptions.GoogleAuthError as e:  # GoogleAuthError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Feedback from {feedback_email}: Firestore auth exception: {e}")
    except google.api_core.exceptions.ClientError as e:  # ClientError(GoogleAPICallError)
        return _abort_return("FIRESTORE FAIL", f"Feedback from {feedback_email}: Firestore client exception: {e}")
    except google.api_core.exceptions.GoogleAPIError as e:  # GoogleAPIError(Exception)
        return _abort_return("FIRESTORE FAIL", f"Feedback from {feedback_email}: Firestore API exception: {e}")
    except Exception as e:
        return _abort_return("FIRESTORE FAIL", f"Feedback from {feedback_email}: Unexpected exception: {e}")

    json_response = {
        "upload": {
            "token": fs_feedback_doc_id
        }
    }

    return jsonify(json_response)
