#!/usr/bin/env python3
"""Tests for to-do list YAML schema and operations."""

import yaml
from datetime import datetime


class TestTodoSchema:
    def test_todos_load(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        assert "todos" in data
        assert len(data["todos"]) == 3

    def test_required_fields(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        for todo in data["todos"]:
            assert "id" in todo
            assert "title" in todo
            assert "priority" in todo
            assert "status" in todo
            assert "created" in todo

    def test_ids_unique(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        ids = [t["id"] for t in data["todos"]]
        assert len(ids) == len(set(ids))

    def test_status_values(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        valid = {"open", "done", "deferred", "cancelled"}
        for todo in data["todos"]:
            assert todo["status"] in valid, f"Bad status: {todo['status']}"

    def test_priority_values(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        valid = {"high", "medium", "low"}
        for todo in data["todos"]:
            assert todo["priority"] in valid, f"Bad priority: {todo['priority']}"

    def test_tags_is_list(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        for todo in data["todos"]:
            if "tags" in todo and todo["tags"] is not None:
                assert isinstance(todo["tags"], list)

    def test_done_has_completed_date(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        done = [t for t in data["todos"] if t["status"] == "done"]
        for todo in done:
            assert todo.get("completed") is not None, f"Done todo {todo['id']} missing completed date"

    def test_open_has_null_completed(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        open_todos = [t for t in data["todos"] if t["status"] == "open"]
        for todo in open_todos:
            assert todo.get("completed") is None, f"Open todo {todo['id']} has completed date"

    def test_one_done_two_open(self, tmp_project_dir):
        with open(tmp_project_dir / "todos.yaml") as f:
            data = yaml.safe_load(f)
        done = [t for t in data["todos"] if t["status"] == "done"]
        open_ = [t for t in data["todos"] if t["status"] == "open"]
        assert len(done) == 1
        assert len(open_) == 2