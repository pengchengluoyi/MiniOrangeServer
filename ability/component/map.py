MAP = {
    "public": {
        "desc": {
            "category": "通用",  # 如果不写，默认就是文件夹名 "web"
            "icon": "cherry",  # 该文件夹下所有组件默认图标
            "color": "#00a6ac"  # 天蓝色
        },
        "details": {
            "trigger": {
                "type": 200,
                "name": "开始",
                "icon": "play",
                "address": "public/trigger",
                "hide": True
            },
            "start": {
                "type": 200,
                "name": "启动/打开",
                "icon": "rocket",
                "address": "public/start",
            },
            "stop": {
                "type": 200,
                "name": "关闭/杀死",
                "icon": "shield-x",  # 对应前端 iconMap 里的图标
                "address": "public/stop",
            },
            "click": {
                "type": 200,
                "name": "点击",
                "icon": "mouse-pointer",
                "address": "public/click",
            },
            "screenshot": {
                "type": 200,
                "name": "截图/截屏",
                "icon": "wallpaper",
                "address": "public/screenshot",
            },
            "gesture": {
                "type": 200,
                "name": "手势/鼠标",
                "icon": "wallpaper",
                "address": "public/gesture",
            },
            "window": {
                "type": 200,
                "name": "窗口",
                "icon": "wallpaper",
                "address": "public/window",
            },
            "input": {
                "type": 200,
                "name": "输入",
                "icon": "wallpaper",
                "address": "public/input",
            },
        }
    },
    "cfs": {
        "desc": {
            "category": "逻辑控制",  # 如果不写，默认就是文件夹名 "web"
            "icon": "Control",  # 该文件夹下所有组件默认图标
            "color": "#54a404"  # 绿色
        },
        "details": {
            "for": {
                "type": 101,
                "name": "循环执行",
                "icon": "if",
                "address": "cfs/mIf",
                "hide": True
            },
            "if": {
                "type": 101,
                "name": "IF判断",
                "icon": "if",
                "address": "cfs/mIf"
            },
            "start": {
                "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
                "name": "等待",
                "icon": "bed",  # 对应前端 iconMap 里的图标
                "address": "cfs/sleep",
            },
        }
    },
    "web": {
        "desc": {
            "category": "网页端",  # 名称
            "icon": "Chrome",  # 该文件夹下所有组件默认图标
            "color": "#ff1486"  # 谷歌红
        },
        "details": {
            "upload": {
                "address": "web/upload_file"
            },
        }
    },
    "mobile": {
        "desc": {
            "category": "移动端",  # 如果不写，默认就是文件夹名 "web"
            "icon": "Smartphone",  # 该文件夹下所有组件默认图标
            "color": "#04dcd8"  # 天蓝色
        },
        "details": {
            "dump_dom": {
                "address": "mobile/dump_hierarchy"
            }
        }
    },
    "api": {
        "desc": {
            "category": "接口",  # 如果不写，默认就是文件夹名 "web"
            "icon": "Cable",  # 该文件夹下所有组件默认图标
            "color": "#fa7000"  # 橙色
        },
        "details": {
            "request": {
                "address": "api/request"
            }
        }
    }
}
