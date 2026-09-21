from pathlib import Path
from flask import Flask
from .database import init_app, initialize_database


def create_app():
    app = Flask(__name__)
    app.config.from_object("experiment.config.Config")
    if not app.config.get("DATABASE_URL"):
        Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    init_app(app)
    with app.app_context():
        initialize_database()
    from .routes import bp
    app.register_blueprint(bp)
    return app
