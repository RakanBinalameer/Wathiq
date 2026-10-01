import re
import pytest
from pathlib import Path
from app import create_app
from app.seed import build_kb, create_user

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("kb") / "t.db"
    con, _ = build_kb(str(path), DATA)
    create_user(con, "nurse1", "Test#Pass123", "ممرضة", "nurse")
    create_user(con, "nurse2", "Test#Pass456", "ممرض", "nurse")
    con.commit(); con.close()
    return str(path)


@pytest.fixture(scope="session")
def app(db_path):
    return create_app({"DATABASE": db_path, "TESTING": True, "RATELIMIT_ENABLED": False})


@pytest.fixture(scope="session")
def kb(app):
    return app.extensions["kb"]


def _login(app, user, pw):
    c = app.test_client()
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": user, "password": pw, "csrf_token": tok})
    assert r.status_code == 302
    home = c.get("/").text
    c.csrf = re.search(r'name="csrf-token" content="([^"]+)"', home).group(1)
    return c


@pytest.fixture()
def client(app):
    return _login(app, "nurse1", "Test#Pass123")


@pytest.fixture()
def client2(app):
    return _login(app, "nurse2", "Test#Pass456")
