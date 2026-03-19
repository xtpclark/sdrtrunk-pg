"""
sdrtrunk-pg Flask application factory.
"""

import logging
from flask import Flask, jsonify

from app import config


def create_app():
    app = Flask(__name__, template_folder="../templates")

    # ----------------------------------------------------------------
    # Logging
    # ----------------------------------------------------------------
    log_level = logging.DEBUG if config.DEBUG else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # ----------------------------------------------------------------
    # Blueprints
    # ----------------------------------------------------------------
    from app.ingest import bp as ingest_bp
    from app.query import bp as query_bp
    from app.merge import bp as merge_bp
    from app.map import bp as map_bp

    app.register_blueprint(ingest_bp)
    app.register_blueprint(query_bp)
    app.register_blueprint(merge_bp)
    app.register_blueprint(map_bp)

    # ----------------------------------------------------------------
    # Health check
    # ----------------------------------------------------------------
    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    # ----------------------------------------------------------------
    # Error handlers
    # ----------------------------------------------------------------
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": "bad request", "detail": str(e)}), 400

    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify({"error": "unauthorized"}), 401

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"error": "internal server error"}), 500

    return app
