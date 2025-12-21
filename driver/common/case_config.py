# !/usr/bin/env python
# -*-coding:utf-8 -*-


class CaseConfig:
    def __init__(self):
        self.TAG = "CaseConfig"
        self.case_name = None
        self.case_description = None
        self.case_author = None
        self.case_author_email = None

        self.case_type = None
        self.case_path = None
        self.case_data = None

        self.case_status = None

    def read_config_file(self):
        ...
