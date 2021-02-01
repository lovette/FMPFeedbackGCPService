"""Flask app for local debug runs.

This module is not used in or by any Cloud Functions.
"""

from flask import Flask, request

from cloudfunctions.fmpfeedback_caretaker import fmpfeedback_caretaker
from cloudfunctions.fmpfeedback_comment import fmpfeedback_comment
from cloudfunctions.fmpfeedback_mailgun import fmpfeedback_mailgun_debug
from cloudfunctions.fmpfeedback_upload import fmpfeedback_upload


app = Flask(__name__)


@app.route('/fmpfeedback_comment', methods=('GET', 'POST'))
def route_fmpfeedback_comment():
    return fmpfeedback_comment(request)


@app.route('/fmpfeedback_upload', methods=('GET', 'POST'))
def route_fmpfeedback_upload():
    return fmpfeedback_upload(request)


@app.route('/fmpfeedback_mailgun_debug', methods=('GET', 'POST'))
def route_fmpfeedback_mailgun_debug():
    return fmpfeedback_mailgun_debug(request)


@app.route('/fmpfeedback_caretaker', methods=('GET', 'POST'))
def route_fmpfeedback_caretaker():
    return fmpfeedback_caretaker(request)
