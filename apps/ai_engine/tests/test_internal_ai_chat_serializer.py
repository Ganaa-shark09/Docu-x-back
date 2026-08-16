import uuid

from django.test import SimpleTestCase

from apps.ai_engine.serializers import AIChatRequestSerializer


class AIChatRequestSerializerTests(SimpleTestCase):
    def test_defaults_to_all_documents_when_no_selection_is_supplied(self):
        serializer = AIChatRequestSerializer(
            data={"message": "What is our leave policy?"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["document_scope"], "all")
        self.assertEqual(serializer.validated_data["document_uuids"], [])

    def test_infers_selected_scope_when_document_uuids_are_supplied(self):
        first = uuid.uuid4()
        second = uuid.uuid4()
        serializer = AIChatRequestSerializer(
            data={
                "message": "Compare these policies.",
                "document_uuids": [str(first), str(second), str(first)],
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["document_scope"], "selected")
        self.assertEqual(serializer.validated_data["document_uuids"], [first, second])

    def test_selected_scope_requires_at_least_one_document(self):
        serializer = AIChatRequestSerializer(
            data={
                "message": "Summarize this document.",
                "document_scope": "selected",
                "document_uuids": [],
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("document_uuids", serializer.errors)

    def test_all_scope_rejects_document_uuids(self):
        serializer = AIChatRequestSerializer(
            data={
                "message": "Question",
                "document_scope": "all",
                "document_uuids": [str(uuid.uuid4())],
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("document_uuids", serializer.errors)
