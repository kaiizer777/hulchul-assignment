"""
backend/tests/test_phase5_adaptability.py
Unit and integration tests for Phase 5 Adaptability (Vendor Filters, Dynamic Approval Thresholds, and Seed Data Variation).
"""

import unittest
import uuid
from unittest.mock import MagicMock

from backend.agent import (
    extract_approval_threshold,
    extract_vendor_filter,
    build_system_prompt,
    parse_amount,
    ReActAgent,
)
from backend.tools import PlaywrightTools


class TestPhase5AdaptabilityUnit(unittest.TestCase):
    """Unit test suite for Phase 5 goal parsing, dynamic threshold extraction, vendor filtering, and prompt adaptability."""

    def test_01_extract_vendor_filter_variations(self):
        """Verify extract_vendor_filter accurately parses vendor names from diverse natural language goal phrasing."""
        # Standard vendor specification matching supported pattern (from vendor / vendor / from)
        self.assertEqual(extract_vendor_filter("Process only invoices from Vendor Acme"), "Acme")
        self.assertEqual(extract_vendor_filter("Process only invoices from vendor Bharat Supplies"), "Bharat Supplies")
        self.assertEqual(extract_vendor_filter("Process invoices from Zenith Parts only"), "Zenith Parts")
        self.assertEqual(extract_vendor_filter("Handle orders from vendor Delta Tech"), "Delta Tech")

        # Edge cases: goals without vendor/from keywords or generic words return None
        self.assertIsNone(extract_vendor_filter("Only process Bharat Supplies"))
        self.assertIsNone(extract_vendor_filter("Process all invoices"))
        self.assertIsNone(extract_vendor_filter("Process any invoice"))
        self.assertIsNone(extract_vendor_filter(""))
        self.assertIsNone(extract_vendor_filter(None))

    def test_02_extract_approval_threshold_variations(self):
        """Verify extract_approval_threshold accurately parses custom thresholds across multiple currency notations and phrasings."""
        # Default fallback when no threshold specified
        self.assertEqual(extract_approval_threshold("Process all pending invoices", default=50000.0), 50000.0)
        self.assertEqual(extract_approval_threshold("", default=50000.0), 50000.0)

        # Custom threshold variations
        self.assertEqual(extract_approval_threshold("Hold anything over ₹25,000 for approval"), 25000.0)
        self.assertEqual(extract_approval_threshold("Flag invoices above Rs 75,000"), 75000.0)
        self.assertEqual(extract_approval_threshold("Hold payments exceeding $30,000"), 30000.0)
        self.assertEqual(extract_approval_threshold("Threshold of INR 100,000 for management signoff"), 100000.0)
        self.assertEqual(extract_approval_threshold("Invoices greater than 15000 need review"), 15000.0)

    def test_03_dynamic_threshold_comparison_logic(self):
        """Verify that agent threshold check correctly respects dynamic thresholds (e.g. ₹25,000 vs default ₹50,000)."""
        agent = ReActAgent(run_id=str(uuid.uuid4()), tools=MagicMock(spec=PlaywrightTools))

        # Dynamic threshold of 25,000
        dyn_threshold = 25000.0
        self.assertTrue(agent.check_amount_exceeds_threshold(25001.0, dyn_threshold))
        self.assertTrue(agent.check_amount_exceeds_threshold("₹28,500", dyn_threshold))
        self.assertFalse(agent.check_amount_exceeds_threshold(25000.0, dyn_threshold))
        self.assertFalse(agent.check_amount_exceeds_threshold("18,000", dyn_threshold))

        # Dynamic threshold of 75,000
        high_threshold = 75000.0
        self.assertTrue(agent.check_amount_exceeds_threshold("Rs. 80,000", high_threshold))
        self.assertFalse(agent.check_amount_exceeds_threshold(75000.0, high_threshold))
        self.assertFalse(agent.check_amount_exceeds_threshold("50,000", high_threshold))

    def test_04_system_prompt_prompt_adaptability_generation(self):
        """Verify system prompt dynamically incorporates extracted vendor filter and custom approval threshold."""
        goal = "Hold anything over ₹25,000 for approval and process only invoices from Vendor Acme"
        threshold = extract_approval_threshold(goal)
        vendor_filter = extract_vendor_filter(goal)

        self.assertEqual(threshold, 25000.0)
        self.assertEqual(vendor_filter, "Acme")

        prompt = build_system_prompt(goal=goal, threshold=threshold, vendor_filter=vendor_filter)
        
        # Assert dynamic details are present in prompt
        self.assertIn("₹25,000.00", prompt)
        self.assertIn("Target Vendor Filter: Only process invoices for vendor 'Acme'", prompt)

    def test_05_arbitrary_synthetic_seed_data_adaptability(self):
        """Verify parsing and evaluation work correctly with arbitrary synthetic seed invoice amounts and vendors."""
        arbitrary_amounts = ["₹12,500.50", "Rs. 99,999.00", "$450.00", 123456.78]
        expected_parsed = [12500.50, 99999.00, 450.00, 123456.78]

        for amt, exp in zip(arbitrary_amounts, expected_parsed):
            self.assertEqual(parse_amount(amt), exp)


if __name__ == "__main__":
    unittest.main()
