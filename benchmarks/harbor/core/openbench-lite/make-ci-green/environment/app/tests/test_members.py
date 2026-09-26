import unittest

from catalog.members import Member


class MembersTest(unittest.TestCase):
    def test_new_member_can_borrow(self):
        m = Member("Ada")
        self.assertTrue(m.can_borrow())

    def test_borrow_up_to_five(self):
        m = Member("Ada")
        for i in range(5):
            m.borrow("b{}".format(i))
        self.assertEqual(len(m.borrowed), 5)

    def test_sixth_borrow_raises(self):
        m = Member("Ada")
        for i in range(5):
            m.borrow("b{}".format(i))
        with self.assertRaises(ValueError):
            m.borrow("b5")


if __name__ == "__main__":
    unittest.main()
