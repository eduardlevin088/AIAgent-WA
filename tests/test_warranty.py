import unittest


from services.warranty import warranty_assessment_message


class WarrantyTests(unittest.TestCase):
    def test_warranty_precheck_not_requested(self):
        self.assertIsNone(warranty_assessment_message("Только спросить статус"))

    def test_warranty_precheck_identifies_non_warranty_reason(self):
        message = "Сломалась ручка по гарантии, изделие упало и есть удар"
        result = warranty_assessment_message(message)
        self.assertIsNotNone(result)
        self.assertIn("не гарант", result.lower())

    def test_warranty_precheck_possible_case(self):
        message = "по гарантии? купил в этом месяце, повреждение само появилось"
        result = warranty_assessment_message(message)
        self.assertIsNotNone(result)
        self.assertIn("потенциально гарантийным", result)


if __name__ == "__main__":
    unittest.main()
