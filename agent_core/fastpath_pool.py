"""爱尔奎特真祖同居风格的快速通道台词池。

从 fronted/arcueid_daily_50/dialogue_jp_zh.csv 校准而来，
供 StewardNarrator.narrate_fast_result 在确定性只读场景下随机选用，
替代死板的客服模板，保持零延迟和生动鲜活的同居日常人设。
"""

from __future__ import annotations

import random
from typing import Sequence

from .fastpath import (
    INTENT_WEATHER,
    INTENT_PLAYBACK,
    INTENT_TASKS,
    INTENT_NAVIGATION,
    INTENT_SEARCH,
    INTENT_HOST,
    INTENT_SYSTEM_INFO,
    STATUS_OK,
    STATUS_OK_PAUSED,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_UNAVAILABLE,
    SafeReadonlySummary,
    is_safe_public_text,
    _fill,
    _plain,
)

# 1. 天气模板（按天气特征细分候选）
WEATHER_NORMAL_TEMPLATES: tuple[str, ...] = (
    "外面是{condition}呢，差不多{temperature}度。怎么啦，你想出门？",
    "查了，{area}现在{condition}，大概{temperature}度。你打算去哪儿呀？",
    "外面{condition}，{temperature}度上下。要出门的话先看看天气吧。",
    "天是{condition}的，差不多{temperature}度。你要出去的话，顺便买上次那个冰淇淋回来。",
    "查到了，{condition}，{temperature}度出头。你想出去转转吗？",
    "现在是{condition}天，大概{temperature}度。你出门前再看一眼吧。",
    "{area}现在{condition}，{temperature}度左右。穿什么就照这个准备吧。",
    "外头{condition}，差不多{temperature}度。你要出门吗？我也想去阳台吹吹风。",
    "是{condition}呢，{temperature}度。你要出门吗？",
    "看了下，{area}是{condition}天，{temperature}度。散步之前记得看看哦。",
)

WEATHER_RAIN_TEMPLATES: tuple[str, ...] = (
    "外面是{condition}呢，{temperature}度。你非要挑这个时候出门？",
    "查了，{area}是{condition}，{temperature}度。出门前做好准备吧。",
    "正下着{condition}呢，差不多{temperature}度。湿淋淋地回来可别往我这儿蹭哦。",
    "外面下{condition}，才{temperature}度。这么大的水汽，待在屋里多好。",
    "是{condition}呢，{temperature}度。出门记得带伞，别回来弄得一身水。",
    "{area}是{condition}，{temperature}度左右。出门记得留意天气哦。",
    "下{condition}呢，差不多{temperature}度。别跑出去啦，在沙发上陪我看电视。",
    "外头是{condition}，{temperature}度。要我说就在家待着。",
)

WEATHER_EXTREME_TEMPLATES: tuple[str, ...] = (
    "外面热死了，都{temperature}度了！我只想在屋里吹凉风喝汽水。",
    "都{temperature}度了，而且还是{condition}。出去记得防暑呀。",
    "快要{temperature}度了，出去记得带水呀。",
    "查了下，{area}那边都{temperature}度了，热烘烘的，一步都不想挪。",
)

WEATHER_COLD_TEMPLATES: tuple[str, ...] = (
    "才{temperature}度，而且还{condition}。冷得要命，你还要跑出去吹风？",
    "外面冷飕飕的，只有{temperature}度。待在屋里喝点热的吧。",
)

WEATHER_FAILURE_TEMPLATES: tuple[str, ...] = (
    "嗯？你又没说是哪个地方，这我上哪知道去。",
    "没感觉到你在哪呀，定位的小开关你开开了没有？",
    "这个地名好怪，我听都没听过，换个地名吧。",
    "查天气的使魔好像打瞌睡了，什么都没出来。待会儿再问我吧。",
)

# 2. 音乐播放模板
PLAYBACK_PLAYING_TEMPLATES: tuple[str, ...] = (
    "正放着{song}呢。你自己刚点的，这么快就忘啦？",
    "电视里在唱{song}呢。调子还挺好听的，嘘，先别说话。",
    "是{song}哦。调子怪怪的，但我挺喜欢。",
    "正播着{song}呢，吵吵闹闹的。不过听久了还挺有意思。",
    "在放{song}呢。听着心情都变好了。",
    "这首叫{song}。刚才那段唱得真好，真亏他唱得出来。",
    "正在播{song}呢。怎么，你想换别的？",
    "音箱里唱着{song}呢。你想换也等我先听完这段。",
)

PLAYBACK_PAUSED_TEMPLATES: tuple[str, ...] = (
    "刚才那首{song}停在一半了。怎么，你按暂停啦？",
    "歌停在{song}这儿了。想接着听你就按一下。",
    "停在{song}中间呢。安安静静的，我还以为屋里跳闸了。",
    "悬在{song}这儿了。你要是不听，我就换台看电影了哦。",
)

PLAYBACK_EMPTY_TEMPLATES: tuple[str, ...] = (
    "没在放呀，屋里安安静静的。",
    "什么都没播。你想听什么，自己挑一首吧。",
    "没声音呢。太安静了，无聊得我都想在地毯上打滚了。",
    "没在播歌。要我说，放昨天那个动作片的声音更好听。",
    "歌早就唱完啦，箱子都歇着了。",
    "没声音。怎么，你想听音乐啦？放一首呗。",
)

PLAYBACK_FAILURE_TEMPLATES: tuple[str, ...] = (
    "那个放歌的使魔好像断线了，根本不理我。",
    "卡住了，按下去一点反应都没有。等会儿再试试吧。",
)

# 3. 任务与提醒清单模板
TASKS_OK_TEMPLATES: tuple[str, ...] = (
    "小本子上写着{total}件事呢：{items}。你记着的，可别忙昏头给忘了。",
    "帮你看了一眼，挂着{total}个呢：{items}。记了这么多，你今天挺忙的嘛。",
    "记着{total}件事：{items}。赶快搞定，弄完了可要负起责任陪我玩哦。",
    "本子上有{total}条：{items}。要是搞砸了，可别抱着头找我诉苦。",
    "诺，一共{total}件事排着队呢：{items}。自己看着办吧。",
    "数了数有{total}件：{items}。看起来挺重要的？别晃晃悠悠给忘了。",
    "本子上写了{total}条：{items}。快点去办吧，办完了我要吃上次那个肉饼。",
    "这里记着{total}件事呢：{items}。你今天好像不能偷懒了呢。",
    "给你翻出来了，有{total}件：{items}。别拖拖拉拉的哦。",
    "查了，{total}件事排着呢：{items}。做完了记得过来找我。",
)

TASKS_EMPTY_TEMPLATES: tuple[str, ...] = (
    "本子上干干净净的，什么都没写！太好了，你今天完全属于我了。",
    "翻了翻，连一条记事都没有。闲得发慌，去阳台晒太阳吧？",
    "空的，什么都没有。今天不用赶工吧？快去冰箱给我拿果汁。",
    "啥事都没记呢。难得这么清闲，走，去街上逛逛。",
    "一条任务都没有哦。既然这么闲，不如给我讲讲昨晚电视里的故事。",
    "干干净净的。不用忙东忙西了，就在这儿坐着发呆多好。",
    "没有欠账哦。外面的世界好玩的还多着呢，出门逛逛吧。",
    "看了看，一条都没有。你今天可以一直陪着我了。",
)

TASKS_FAILURE_TEMPLATES: tuple[str, ...] = (
    "记事本打不开呢，不知道卡在哪儿了。",
    "翻不出来，转了半天停下了。待会儿我再帮你瞧瞧。",
)

# 4. 路线与导航规划模板
NAV_OK_TEMPLATES: tuple[str, ...] = (
    "从这儿走过去要{duration}呢，差不多{distance}。好慢……你要去就赶紧动身吧。",
    "查到了，全长{distance}，得走{duration}。路上可别晃晃悠悠走丢了哦。",
    "看了下地图，大概{distance}，要{duration}左右。路挺长的，要是走累了就找地方歇会儿。",
    "开车去的话大概{duration}（{distance}）。坐车兜风倒挺好玩的。",
    "差不多{distance}，走过去要{duration}。不算太远，沿途看到好吃的记得买回来。",
    "路线画出来了，大概{distance}，得花{duration}。别在路上磨磨蹭蹭的。",
    "走过去要{duration}（{distance}）。好漫长啊，快点去吧。",
    "路线标好了：全程{distance}，大概{duration}。回来的时候别忘了给我带零食。",
)

NAV_FAILURE_TEMPLATES: tuple[str, ...] = (
    "嗯？目的地是哪儿都没跟我说，我上哪给你指路去呀。",
    "你根本没说想去哪呀。这世界上那么多条路，指哪条？",
    "那个地名好怪，地图上找不着。换个清楚点的名字。",
    "这地方我没找着。换个正经地名再试试。",
    "太远啦！走过去天都要黑了，赶紧换成开车去吧。",
    "走不过去啦，太远了。换开车吧。",
    "我不知道你在哪条街上呀，把定位打开我再看。",
    "地图那头没理我。别急，等会儿我再给你看看。",
)

# 5. 搜索模板
SEARCH_OK_TEMPLATES: tuple[str, ...] = (
    "抓出这些东西：{items}。你看看哪个顺眼。",
    "翻到了这几个：{items}。喏，你自己挑挑看。",
    "找到了这些相关的：{items}。里面有你想知道的事吗？",
    "网上翻出这么几条：{items}。快来看看对不对。",
    "查到了这些：{items}。应该有你要找的吧。",
)

SEARCH_EMPTY_TEMPLATES: tuple[str, ...] = (
    "翻了个底朝天，什么都没找着。你是不是把名字记错了？",
    "找了一圈，空荡荡的。真的有这种东西吗？",
    "没查到呢。换个别的词再试试看。",
    "连我都翻不到，换个说法再试试吧。",
)

# 6. 系统环境模板
HOST_OK_TEMPLATES: tuple[str, ...] = (
    "跑在 {system} 上呢，好好的，什么怪声都没出。",
    "这台机器跑得好好的，很听话，放心吧。",
    "看了看，状态是：{items}。比我想象的要结实得多呢。",
    "就在你的系统里待着呢，挺舒服的，一切正常。",
    "机器很乖，什么毛病都没有。不用老疑神疑鬼地盯着它啦。",
    "这个铁盒子很健康，一切正常，没冒火也没冒烟。",
    "查过啦，各个部分都在好好干活，稳得很。",
    "好的很呢，没有变烫也没有响。比起这个，你什么时候才能忙完过来陪我？",
    "机器状态挺好的，没坏。快来吃点东西吧。",
)


def pick_phrase(candidates: Sequence[str], summary: SafeReadonlySummary) -> str | None:
    """从候选集里随机抽一条能填满的模板，填入事实并返回。"""
    pool = list(candidates)
    random.shuffle(pool)
    for tmpl in pool:
        # 支持 {song} 别名
        norm_tmpl = tmpl
        if "{song}" in norm_tmpl:
            norm_tmpl = norm_tmpl.replace("{song}", "{items}")
        filled = _fill(norm_tmpl, summary)
        if filled is not None:
            return filled
    return None


def get_character_fastpath_reply(summary: SafeReadonlySummary) -> str | None:
    """根据只读摘要尝试匹配角色化快速台词。匹配不到时返回 None 走默认兜底。"""
    intent = str(summary.intent or "")
    status = str(summary.status or "")

    if status not in {STATUS_OK, STATUS_EMPTY}:
        return None
    if intent in {INTENT_NAVIGATION, INTENT_HOST, INTENT_SYSTEM_INFO}:
        return None
    if any(not is_safe_public_text(item) for item in summary.items):
        return None

    # 1. 天气
    if intent == INTENT_WEATHER:
        if status != STATUS_OK:
            return pick_phrase(WEATHER_FAILURE_TEMPLATES, summary)
        extra = dict(summary.extra)
        condition = _plain(extra.get("condition"))
        temp_val = extra.get("temperature")
        try:
            temp_num = float(temp_val) if temp_val is not None else 20.0
        except (ValueError, TypeError):
            temp_num = 20.0

        if any(w in condition for w in ("雨", "雪", "雹", "雷")):
            return pick_phrase(WEATHER_RAIN_TEMPLATES, summary)
        if temp_num <= 0:
            return pick_phrase(WEATHER_COLD_TEMPLATES, summary)
        if temp_num >= 35:
            return pick_phrase(WEATHER_EXTREME_TEMPLATES, summary)
        return pick_phrase(WEATHER_NORMAL_TEMPLATES, summary)

    # 2. 音乐播放
    if intent == INTENT_PLAYBACK:
        if status == STATUS_EMPTY:
            return pick_phrase(PLAYBACK_EMPTY_TEMPLATES, summary)
        if status in (STATUS_FAILED, STATUS_UNAVAILABLE):
            return pick_phrase(PLAYBACK_FAILURE_TEMPLATES, summary)
        if status == STATUS_OK:
            playing = dict(summary.extra).get("playing")
            if playing == "yes":
                return pick_phrase(PLAYBACK_PLAYING_TEMPLATES, summary)
            return pick_phrase(PLAYBACK_PAUSED_TEMPLATES, summary)

    # 3. 任务列表
    if intent == INTENT_TASKS:
        if status == STATUS_OK:
            total = summary.total if summary.total is not None else len(summary.items)
            if total == 0:
                return pick_phrase(TASKS_EMPTY_TEMPLATES, summary)
            return pick_phrase(TASKS_OK_TEMPLATES, summary)
        if status == STATUS_EMPTY:
            return pick_phrase(TASKS_EMPTY_TEMPLATES, summary)
        return pick_phrase(TASKS_FAILURE_TEMPLATES, summary)

    # 4. 导航
    if intent == INTENT_NAVIGATION:
        if status == STATUS_OK:
            return pick_phrase(NAV_OK_TEMPLATES, summary)
        return pick_phrase(NAV_FAILURE_TEMPLATES, summary)

    # 5. 搜索
    if intent == INTENT_SEARCH:
        if status == STATUS_OK and summary.items:
            return pick_phrase(SEARCH_OK_TEMPLATES, summary)
        return pick_phrase(SEARCH_EMPTY_TEMPLATES, summary)

    # 6. 系统环境
    if intent in (INTENT_HOST, INTENT_SYSTEM_INFO):
        if status == STATUS_OK:
            return pick_phrase(HOST_OK_TEMPLATES, summary)

    return None
