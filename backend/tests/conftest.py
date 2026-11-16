import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["APP_ENV"] = "test"
os.environ["DEBUG"] = "true"

from app.db import Base
from app.models import (
    ConsentRecord,
    Contact,
    EmailTemplate,
    normalize_email,
)
from app.settings import settings


@pytest.fixture(scope="session")
def engine():
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=test_engine)
    return test_engine


@pytest.fixture
def db(engine):
    connection = engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection, expire_on_commit=False)
    session = session_factory()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def sample_template(db):
    template = EmailTemplate(
        name="Standard Welcome",
        subject="Welcome to our newsletter",
        sender_name="Acme News",
        sender_email="news@example.com",
        mailing_address="123 Example Street, City, Country",
        html_body="<p>Welcome! <a href='{{unsubscribe_url}}'>Unsubscribe</a></p>",
        text_body="Welcome! Unsubscribe here: {{unsubscribe_url}}",
    )
    db.add(template)
    db.commit()
    return template


@pytest.fixture
def sample_contact(db):
    contact = Contact(
        email="subscriber@example.com",
        email_normalized=normalize_email("subscriber@example.com"),
        display_name="Sample Subscriber",
    )
    db.add(contact)
    db.flush()

    consent = ConsentRecord(
        contact_id=contact.id,
        source="website_signup",
        consented_at=settings.model_dump().get("created_at") or contact.created_at,
        purpose="monthly_newsletter",
        provenance="Web form ip=127.0.0.1",
    )
    db.add(consent)
    db.commit()
    return contact


@pytest.fixture(autouse=True)
def reset_global_in_memory_limiter():
    """Ensure in-memory token buckets are pristine for every test case."""
    from app.throttling import _GLOBAL_IN_MEMORY_LIMITER

    _GLOBAL_IN_MEMORY_LIMITER.reset()
    yield
    _GLOBAL_IN_MEMORY_LIMITER.reset()


@pytest.fixture
def make_request():
    """Create a lightweight Starlette Request without network sockets.

    Prevents Windows Proactor/SelectorEventLoop _fallback_socketpair deadlocks.
    """
    from starlette.requests import Request

    def _factory(
        method: str = "GET",
        path: str = "/",
        headers: list[tuple[bytes, bytes]] | None = None,
        body: bytes = b"",
        client: tuple[str, int] = ("127.0.0.1", 54321),
    ) -> Request:
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method.upper(),
            "path": path,
            "raw_path": path.encode("utf-8"),
            "headers": headers or [(b"host", b"testserver")],
            "query_string": b"",
            "client": client,
            "server": ("testserver", 80),
            "state": {},
        }

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(scope, receive)

    return _factory
