from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_moment_requirements_are_the_single_version_source() -> None:
    requirements = (PROJECT_ROOT / "requirements-moment.txt").read_text(encoding="utf-8")

    for requirement in (
        "numpy==1.25.2",
        "scipy==1.11.4",
        "torch==2.13.0",
        "transformers==5.15.0",
        "accelerate==1.14.0",
        "safetensors==0.8.0",
    ):
        assert requirement in requirements

    expected_references = {
        "requirements.txt": "-r requirements-moment.txt",
        "edge_service/requirements.txt": "-r ../requirements-moment.txt",
        "sender_module/requirements.txt": "-r ../requirements-moment.txt",
    }
    for relative_path, expected in expected_references.items():
        assert (PROJECT_ROOT / relative_path).read_text(encoding="utf-8").strip() == expected
