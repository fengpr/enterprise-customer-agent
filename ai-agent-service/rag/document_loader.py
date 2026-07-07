from pathlib import Path


class DocumentLoader:
    """知识库文档加载器，负责把上传文件转换为可切分文本。"""

    def load_text(self, path: str) -> str:
        """加载本地文本类文档；不支持格式直接失败，避免错误解析进入知识库。"""
        file_path = Path(path)
        if file_path.suffix.lower() not in {".txt", ".md"}:
            raise ValueError("Demo loader currently supports .txt and .md files only.")
        return file_path.read_text(encoding="utf-8")
