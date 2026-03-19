"""
Entry point for running the sdrtrunk-pg Flask app directly.

    python run.py

For production, use gunicorn:
    gunicorn -w 2 -b 0.0.0.0:5010 "app:create_app()"
"""

from app import create_app
from app.config import DEBUG, PORT

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
