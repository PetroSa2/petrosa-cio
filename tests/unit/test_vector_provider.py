from cio.core.vector import MockVectorClient
from cio.main import create_vector_client


def test_qdrant_provider_falls_back_to_mock(caplog):
    caplog.set_level("ERROR")

    client = create_vector_client("qdrant")

    assert isinstance(client, MockVectorClient)
    assert "VECTOR_PROVIDER=qdrant is not supported" in caplog.text
