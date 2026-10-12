"""Re-embed existing 1024D knowledge chunks without changing the live index.

Run inside the backend container before deployment; after complete, run with
--activate immediately before switching the backend to the gateway image.
The original embeddings remain as embedding_legacy for rollback.
"""

import os
import sys

import psycopg2
import requests
from sqlalchemy.engine.url import make_url


MODEL = "text-embedding-v3"


def connect():
    url = make_url(os.environ["DATABASE_URL"])
    return psycopg2.connect(
        host=url.host, port=url.port, user=url.username,
        password=url.password, dbname=url.database,
    )


def stage():
    key = os.environ.get("AI_GATEWAY_API_KEY")
    url = os.environ.get("AI_GATEWAY_BASE_URL", "").rstrip("/") + "/embeddings"
    if not key or not url:
        raise RuntimeError("AI_GATEWAY_API_KEY / AI_GATEWAY_BASE_URL 未配置")
    db = connect()
    client = requests.Session()
    client.trust_env = False
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='vector_chunks' AND column_name='embedding_legacy'")
            if cursor.fetchone()[0]:
                print("Gateway embedding index is already active", flush=True)
                return
            cursor.execute("ALTER TABLE vector_chunks ADD COLUMN IF NOT EXISTS embedding_gateway vector(1024)")
            db.commit()
        processed = 0
        while True:
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT id, content FROM vector_chunks WHERE embedding_gateway IS NULL "
                    "ORDER BY id LIMIT 8"
                )
                rows = cursor.fetchall()
            if not rows:
                break
            response = client.post(
                url, headers={"Authorization": f"Bearer {key}"},
                json={"model": MODEL, "input": [row[1] for row in rows]}, timeout=90,
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            if len(data) != len(rows) or any(len(item["embedding"]) != 1024 for item in data):
                raise ValueError("网关嵌入模型维度或批次长度不符")
            with db.cursor() as cursor:
                for (row_id, _), item in zip(rows, data):
                    vector = "[" + ",".join(str(value) for value in item["embedding"]) + "]"
                    cursor.execute(
                        "UPDATE vector_chunks SET embedding_gateway=%s::vector WHERE id=%s",
                        (vector, row_id),
                    )
                db.commit()
            processed += len(rows)
            if processed % 80 == 0:
                print(f"Re-embedded {processed} chunks", flush=True)
        print(f"Staging complete: {processed} chunks", flush=True)
    finally:
        client.close()
        db.close()


def activate():
    db = connect()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='vector_chunks' AND column_name='embedding_legacy'")
            if cursor.fetchone()[0]:
                print("Gateway embedding index is already active", flush=True)
                return
            cursor.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='vector_chunks' AND column_name='embedding_gateway'")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("请先完成向量迁移再激活")
            cursor.execute("LOCK TABLE vector_chunks IN ACCESS EXCLUSIVE MODE")
            cursor.execute("SELECT count(*) FROM vector_chunks WHERE embedding_gateway IS NULL")
            if cursor.fetchone()[0]:
                raise RuntimeError("仍有旧向量未迁移，拒绝切换")
            cursor.execute("DROP INDEX IF EXISTS idx_vchunks_hnsw")
            cursor.execute("ALTER TABLE vector_chunks RENAME COLUMN embedding TO embedding_legacy")
            cursor.execute("ALTER TABLE vector_chunks ALTER COLUMN embedding_legacy DROP NOT NULL")
            cursor.execute("ALTER TABLE vector_chunks RENAME COLUMN embedding_gateway TO embedding")
            cursor.execute("ALTER TABLE vector_chunks ALTER COLUMN embedding SET NOT NULL")
            cursor.execute("CREATE INDEX idx_vchunks_hnsw ON vector_chunks USING hnsw (embedding vector_cosine_ops)")
        db.commit()
        print("Gateway embedding index activated", flush=True)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    if "--activate" in sys.argv:
        activate()
    else:
        stage()
