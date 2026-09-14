# Shared extension instances -- imported by models.py and create_app rather
# than instantiated in either, so there's exactly one SQLAlchemy registry.
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()
