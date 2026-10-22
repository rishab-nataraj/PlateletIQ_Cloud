import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Supabase PostgreSQL in the cloud (DATABASE_URL); SQLite for offline/local dev.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./plateletiq.db"
)


def _normalise(url: str) -> str:
    """
    Pin PostgreSQL URLs to the psycopg 3 driver.

    Hosts hand out ``postgres://`` or ``postgresql://`` URLs; which driver
    SQLAlchemy picks for those changed between 2.0 (psycopg2) and 2.1
    (psycopg). Naming it explicitly makes the deployment independent of that.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _normalise(DATABASE_URL)

connect_args = {}
engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    # SQLite needs check_same_thread=False; background delivery threads share it.
    connect_args = {"check_same_thread": False, "timeout": 15}
else:
    # Supabase's pooler (Supavisor) in transaction mode cannot keep server-side
    # prepared statements between transactions, so never create them.
    connect_args = {"prepare_threshold": None}
    # Free-tier databases allow few connections; recycle before the pooler drops them.
    engine_options.update(pool_size=5, max_overflow=5, pool_recycle=300)

engine = create_engine(DATABASE_URL, connect_args=connect_args, **engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
