# !/usr/bin/env python
# -*-coding:utf-8 -*-

MCFS_IF = 101
MCFS_WHILE = 102
MCFS_FOR = 110
MCFS_FOR_CHILD = 111
MCFS_FOR_RANDOM = 112
MCFS_FOR_ELEMENT = 120

MN_NORMAL = 200

MTYPE_CFS = [MCFS_FOR_RANDOM, MCFS_FOR_ELEMENT, MCFS_WHILE, MCFS_IF, MCFS_FOR, MCFS_FOR_CHILD]
MTYPE_NORMAL = [MN_NORMAL]



PASS: str = "pass"
FAIL: str = "fail"

RECENT_OPEN: str = "open"
RECENT_SWITCH: str = "switch"
RECENT_CLOSE: str = "close"

C_M_GET_ELEMENTS: str = "m_g_e"
C_M_LIST_ELEMENTS: str = "m_l_e"
C_M_I_ELEMENTS: str = "m_i_e"