"""A tiny task-list CLI."""
import argparse
import json


def list_tasks(tasks, status="all"):
    # Filtering is not wired up yet.
    return tasks


def main():
    parser = argparse.ArgumentParser(description="List project tasks")
    parser.add_argument("file")
    parser.add_argument("--status", choices=["all", "open", "done"], default="all")
    args = parser.parse_args()
    with open(args.file) as source:
        tasks = json.load(source)
    for task in list_tasks(tasks, args.status):
        print(f"[{task['status']}] {task['title']}")


if __name__ == "__main__":
    main()
