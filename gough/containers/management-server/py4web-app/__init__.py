from py4web import action, request, abort, redirect, URL, Field
from py4web.utils.form import Form, FormStyleBulma
from py4web.utils.publisher import Publisher, ALLOW_ALL_POLICY
from pydal import Database
import os
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection
db = Database(
    f"postgres://postgres:postgres@postgres:5432/management",
    folder="databases",
    pool_size=10
)

# Import controllers
from .controllers import default, maas_api, servers, cloud_init, packages