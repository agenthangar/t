import unittest
from tasks import list_tasks


class TaskFilterTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [
            {"title": "Ship the CLI", "status": "open"},
            {"title": "Write the docs", "status": "done"},
            {"title": "Test desktop handoff", "status": "open"},
        ]

    def test_all_tasks(self):
        self.assertEqual(list_tasks(self.tasks), self.tasks)

    def test_open_tasks(self):
        self.assertEqual([t["title"] for t in list_tasks(self.tasks, "open")],
                         ["Ship the CLI", "Test desktop handoff"])

    def test_done_tasks(self):
        self.assertEqual([t["title"] for t in list_tasks(self.tasks, "done")],
                         ["Write the docs"])


if __name__ == "__main__":
    unittest.main()
