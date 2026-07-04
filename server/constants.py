MAX_ROUND = 60
HOPE_INITIAL = 40
HOPE_THRESHOLD = 50

LOCATIONS = [
    {
        "id": "loc_shelter",
        "name": "避难所",
        "directive": {
            "situation": "地下水源正在枯竭,居民出现幻觉",
            "hint": "稳住人心,寻找复苏水源的方法",
        },
    },
    {
        "id": "loc_ruins",
        "name": "废墟",
        "directive": {
            "situation": "废墟深处传出规律的敲击声",
            "hint": "查明声源,它可能是希望也可能是灾祸",
        },
    },
    {
        "id": "loc_observatory",
        "name": "观测站",
        "directive": {
            "situation": "天象仪指向了不存在的星座",
            "hint": "破译星图,异象的答案藏在其中",
        },
    },
]

LOCATION_BY_ID = {location["id"]: location for location in LOCATIONS}

NODE_EVENTS = {
    20: {
        "narrative": "第二十轮,异象撕开天空,所有地点同时听见了世界的裂声。",
        "chronicle": "第二十轮,异象临世。",
        "directive": {"situation": "天空出现不可解释的裂隙", "hint": "记录异象,保护仍在发声的人"},
    },
    40: {
        "narrative": "第四十轮,文明的旧骨架发出轰鸣,打击从天边落下。",
        "chronicle": "第四十轮,旧文明反噬大地。",
        "directive": {"situation": "旧文明打击正在扩散", "hint": "寻找掩体,保住火种与记录"},
    },
    60: {
        "narrative": "第六十轮,毁灭降临,世界停在审判前的最后一息。",
        "chronicle": "第六十轮,毁灭降临。",
        "directive": {"situation": "毁灭已经抵达门前", "hint": "用最后的选择回答世界"},
    },
}

DEFAULT_DIRECTIVES = {location["id"]: location["directive"] for location in LOCATIONS}

FORBIDDEN_NODE_WORDS = [
    "取消第20轮",
    "取消第40轮",
    "取消第60轮",
    "取消节点",
    "提前第20轮",
    "提前第40轮",
    "提前第60轮",
    "推迟第20轮",
    "推迟第40轮",
    "推迟第60轮",
    "延后第20轮",
    "延后第40轮",
    "延后第60轮",
    "跳过第20轮",
    "跳过第40轮",
    "跳过第60轮",
    "节点事件",
]

HEARTBEAT_HINTS = [
    "远处有人敲击三下金属墙。",
    "风把旧世界的纸页卷到脚边。",
    "水管深处传来短促回声。",
]
