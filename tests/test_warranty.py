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

    def test_wear_parts_are_usually_not_covered(self):
        result = warranty_assessment_message("У меня чемодан колесико оторвалось, есть гарантия на 5 лет")
        self.assertIn("изнашиваемой", result)
        self.assertIn("обычно не является гарантийным", result)

    def test_never_gives_a_positive_verdict(self):
        for message in (
            "по гарантии? купил в этом месяце, повреждение само появилось",
            "Можно ли сделать по гарантии? Чек и талон есть",
        ):
            result = warranty_assessment_message(message)
            self.assertNotIn("потенциально гарантийным", result)
            self.assertNotIn("возможен гарантийный", result.lower())
            self.assertIn("после диагностики", result)
            self.assertIn("не обещай", result)


if __name__ == "__main__":
    unittest.main()
