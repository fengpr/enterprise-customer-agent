import argparse
import json
from pathlib import Path
from typing import Any

from rag.document_loader import DocumentLoader
from rag.text_processing import split_into_chunks


def main() -> None:
    """本地知识文档预处理入口，输出可入库的 chunk JSON。"""
    parser = argparse.ArgumentParser(description="Preprocess knowledge documents into metadata-rich chunks.")
    parser.add_argument("--path", required=True, help="知识文档文件或目录，第一版支持 .md/.txt")
    parser.add_argument("--collection", required=True, help="知识集合，例如 refund_policy")
    parser.add_argument("--business-scope", required=True, help="业务范围，例如 refund")
    parser.add_argument("--version", required=True, help="文档版本，例如 V1.0")
    parser.add_argument("--risk-level", default="low", choices=["low", "medium", "high", "critical"])
    parser.add_argument("--source-type", default="official_policy")
    parser.add_argument("--status", default="PUBLISHED")
    parser.add_argument("--answerable-intents", default="", help="逗号分隔的可回答意图")
    args = parser.parse_args()

    chunks = ingest_path(
        path=args.path,
        collection=args.collection,
        business_scope=args.business_scope,
        version=args.version,
        risk_level=args.risk_level,
        source_type=args.source_type,
        status=args.status,
        answerable_intents=[item.strip() for item in args.answerable_intents.split(",") if item.strip()],
    )
    print(json.dumps(chunks, ensure_ascii=False, indent=2))


def ingest_path(
    *,
    path: str,
    collection: str,
    business_scope: str,
    version: str,
    risk_level: str = "low",
    source_type: str = "official_policy",
    status: str = "PUBLISHED",
    answerable_intents: list[str] | None = None,
) -> list[dict[str, Any]]:
    """读取文件或目录并输出带 metadata 的 chunk，后续可接数据库入库。"""
    loader = DocumentLoader()
    root = Path(path)
    files = [root] if root.is_file() else sorted(item for item in root.rglob("*") if item.suffix.lower() in {".md", ".txt"})
    all_chunks: list[dict[str, Any]] = []
    for file_path in files:
        text = loader.load_text(str(file_path))
        chunks = split_into_chunks(
            text,
            doc_name=file_path.stem,
            version=version,
            collection=collection,
            business_scope=business_scope,
            status=status,
            risk_level=risk_level,
            answerable_intents=answerable_intents or [],
            source_type=source_type,
        )
        for chunk in chunks:
            payload = {
                "doc_name": chunk.doc_name,
                "version": chunk.version,
                "paragraph": chunk.paragraph,
                "collection": chunk.collection,
                "business_scope": chunk.business_scope,
                "heading_path": chunk.heading_path,
                "status": chunk.status,
                "risk_level": chunk.risk_level,
                "answerable_intents": chunk.answerable_intents,
                "source_type": chunk.source_type,
                "chunk_index": chunk.chunk_index,
            }
            all_chunks.append(payload)
    return all_chunks


if __name__ == "__main__":
    main()
