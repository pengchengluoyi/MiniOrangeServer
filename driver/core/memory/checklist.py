# !/usr/bin/env python
# -*-coding:utf-8 -*-
import copy
from script.log import SLog
from driver.common.task_details import TaskDetails

TAG = "Checklist"


class Checklist:

    def __init__(self):
        self.checklist = {}
        self.current_node = None

    def create(self, checklist):
        """
        Push a message onto the queue.
        """
        for key, value in checklist.items():
            if key == "_ui_meta": continue
            self.checklist[key] = TaskDetails(value)

    def peek(self):
        """
        Peek at the oldest message without removing it from the queue.
        """
        if self.current_node is None:
            trigger = [
                value
                for key, value in self.checklist.items()
                if key.startswith("public-trigger")
            ]
            self.current_node = trigger[0]
        else:
            next_nodes = self.current_node.nextCodes
            if len(next_nodes) != 0:
                self.current_node = copy.deepcopy(self.checklist[next_nodes[0]])
            else:
                return None
        return self.current_node
