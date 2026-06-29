from apps.documents.models import Document


class DocumentClassificationService:
    KEYWORDS = {
        Document.DocumentType.INVOICE: [
            "invoice",
            "bill to",
            "amount due",
            "tax invoice",
            "gst",
            "vat",
        ],
        Document.DocumentType.CONTRACT: [
            "agreement",
            "contract",
            "terms and conditions",
            "party",
            "termination",
            "jurisdiction",
        ],
        Document.DocumentType.POLICY: [
            "policy",
            "procedure",
            "guideline",
            "compliance",
        ],
        Document.DocumentType.RESUME: [
            "resume",
            "curriculum vitae",
            "experience",
            "education",
            "skills",
        ],
        Document.DocumentType.PURCHASE_ORDER: [
            "purchase order",
            "po number",
            "ship to",
            "vendor",
        ],
        Document.DocumentType.BANK_STATEMENT: [
            "bank statement",
            "account number",
            "transaction",
            "opening balance",
            "closing balance",
        ],
    }

    @classmethod
    def classify(cls, document):
        extracted_text = getattr(document, "text_extraction", None)

        if not extracted_text or not extracted_text.extracted_text:
            return {
                "document_type": document.document_type,
                "confidence": 0,
                "matched_keywords": [],
            }

        text = extracted_text.extracted_text.lower()
        scores = {}

        for document_type, keywords in cls.KEYWORDS.items():
            matched = [keyword for keyword in keywords if keyword in text]
            scores[document_type] = {
                "score": len(matched),
                "matched_keywords": matched,
            }

        best_type = max(scores, key=lambda key: scores[key]["score"])
        best_score = scores[best_type]["score"]

        if best_score == 0:
            return {
                "document_type": document.document_type,
                "confidence": 0,
                "matched_keywords": [],
            }

        document.document_type = best_type
        document.save(update_fields=["document_type", "updated_at"])

        return {
            "document_type": best_type,
            "confidence": min(best_score * 20, 95),
            "matched_keywords": scores[best_type]["matched_keywords"],
        }
