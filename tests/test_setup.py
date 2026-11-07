"""Phase 2 sanity checks: environment, dependencies and package layout."""
import importlib

import pytest

DEPENDENCIES = ["requests", "bs4", "lxml", "trafilatura", "readability", "docx", "dotenv"]
MODULES = [
    "src",
    "src.article_extractor",
    "src.content_processor",
    "src.notes_generator",
    "src.word_generator",
    "src.main",
    "src.extractors",
    "src.backends",
    "src.notes_model",
    "src.backends.base",
    "src.backends.local",
    "src.backends.local_questions",
    "src.backends.llm",
    "src.backends.ollama",
    "src.backends.openai_backend",
]


@pytest.mark.parametrize("name", DEPENDENCIES)
def test_dependency_importable(name: str) -> None:
    importlib.import_module(name)


@pytest.mark.parametrize("name", MODULES)
def test_project_module_importable(name: str) -> None:
    importlib.import_module(name)
