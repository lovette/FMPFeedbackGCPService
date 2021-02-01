"""Flask app entry point for local debug runs.

This module is not used in or by any Cloud Functions.
"""

from app import app


if __name__ == '__main__':
    app.run(debug=True, use_reloader=True, port=8000)
