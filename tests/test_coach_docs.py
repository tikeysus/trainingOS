"""Tests that documentation meets issue requirements."""

from __future__ import annotations

import unittest
from pathlib import Path


class CoachUIDocumentationTests(unittest.TestCase):
    """Verify that coach UI documentation explains privacy implications.

    Issue #26 acceptance criteria requires: "Docs explain the privacy
    implications of exposing the coach UI."
    """

    def test_coach_docs_explain_privacy_implications_of_network_exposure(self) -> None:
        """Coach documentation must explain why localhost-only is the default."""
        coach_docs = Path(__file__).parent.parent / "docs" / "coach.md"
        self.assertTrue(coach_docs.exists(), f"Coach docs not found at {coach_docs}")

        content = coach_docs.read_text()

        # Check for required sections and keywords
        required_keywords = [
            "localhost",  # Default binding
            "127.0.0.1",  # Explicit localhost address
            "external",  # Network exposure concept
            "privacy",  # Privacy implications
            "TRAININGOS_COACH_ALLOW_EXTERNAL",  # The opt-in flag
        ]

        for keyword in required_keywords:
            self.assertIn(
                keyword,
                content,
                f"Coach documentation must mention '{keyword}' to explain network exposure",
            )

    def test_coach_docs_explain_bearer_token_usage(self) -> None:
        """Coach documentation must explain token-based authentication."""
        coach_docs = Path(__file__).parent.parent / "docs" / "coach.md"
        content = coach_docs.read_text()

        # Check for token-related documentation
        required_keywords = [
            "TRAININGOS_COACH_TOKEN",  # The environment variable
            "Bearer",  # HTTP auth scheme
            "authentication",  # Auth concept
        ]

        for keyword in required_keywords:
            self.assertIn(
                keyword,
                content,
                f"Coach documentation must mention '{keyword}' to explain token authentication",
            )

    def test_coach_docs_explain_risk_of_external_exposure(self) -> None:
        """Documentation must explain what data is at risk if exposed."""
        coach_docs = Path(__file__).parent.parent / "docs" / "coach.md"
        content = coach_docs.read_text()

        # Should mention concrete data at risk
        risk_keywords = [
            "history",  # Training history
            "data",  # Generic data exposure
        ]

        risk_found = any(keyword in content.lower() for keyword in risk_keywords)
        self.assertTrue(
            risk_found,
            "Coach documentation should explain what training data is at risk if UI is exposed",
        )

    def test_coach_docs_show_external_usage_example(self) -> None:
        """Documentation should provide example configuration for external access."""
        coach_docs = Path(__file__).parent.parent / "docs" / "coach.md"
        content = coach_docs.read_text()

        # Should show example with both flags set
        self.assertIn(
            "TRAININGOS_COACH_ALLOW_EXTERNAL",
            content,
            "Docs should show example of setting TRAININGOS_COACH_ALLOW_EXTERNAL",
        )


if __name__ == "__main__":
    unittest.main()
