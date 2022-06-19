import json
import os
import re
import threading
import time
from random import random

import aria2rpc
import pymysql
import requests
from loguru import logger
from tmdbv3api import TMDb, TV, Season, Episode

from AnimeEpisode import AnimeEpisode
from DBUtil import DBUtil
from config import *
from utils import parse_num, parse_bangumi_tag

request_head = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.4 Safari/605.1.15"
}

db_util = None


def get_follow_list():
    """
    获取追番列表

    :return:
    """
    global db_util
    result = db_util.get_follows()
    follow_list = []
    logger.info("找到追番列表：")
    for r in result:
        logger.info("\t" + r[1])
        follow_list.append(
            AnimeEpisode(tm_id=r[0], name=r[1], team=r[3], bangumi_tag=r[4], season=r[5], language=r[6]))
    return follow_list


def get_local_episodes(anime: AnimeEpisode):
    """
    获取本地已存储的剧集列表
    如果没有本地文件夹会在LOCAL_PATH第一个目录下创建

    :param anime: 要查找的动漫对象
    :return List{int}:
    """
    local_anime = []
    for path in LOCAL_PATH:
        try:
            for anime_name in os.listdir(path):
                if re.search("^" + anime.name + " ?(\(\d{4}\))?", anime_name):
                    anime.path = path + '/' + anime_name + "/Season " + str(anime.season)
                    if not os.path.isdir(anime.path):
                        os.mkdir(anime.path)
                    season_dir = os.listdir(anime.path)
                    for e_file in season_dir:
                        temp = re.findall("S0?" + str(anime.season) + "E(\d+).*?\.(mp4|mkv)$", e_file)
                        if len(temp) > 0:
                            local_anime.append(int(temp[0][0]))
                    break

        except FileNotFoundError as fe:
            print(fe)
    # 本地还没有动漫文件夹
    if anime.path is None:
        os.mkdir("{}/{}".format(LOCAL_PATH[0], anime.name))
        return get_local_episodes(anime)
    return local_anime


def get_tmdb_data(anime: AnimeEpisode):
    """
    获取tmdb的数据

    :param anime: 动漫对象
    :return: 所有已经发布但未下载但剧集对象
    """
    global db_util
    prepare_list = []
    local_episodes = get_local_episodes(anime)
    tmdb = TMDb()
    tmdb.language = "zh"
    tmdb.api_key = TheMovieDBKey
    anime.set_anime_data(TV().details(anime.tm_id))
    anime.set_season_data(Season().details(anime.tm_id, anime.season))
    for e in anime.tmdb["season"]["episodes"]:
        if e['air_date'] != "" and time.mktime(time.strptime(e['air_date'], '%Y-%m-%d')) < time.time() and \
                int(e['episode_number']) not in local_episodes:
            e = AnimeEpisode(anime.name, anime.season, int(e["episode_number"]), anime.path, anime.tm_id,
                             anime.language,
                             None, None, anime.tmdb, anime.bangumi_tag)
            e.set_episode_data(Episode().details(e.tm_id, e.season, e.episode))
            prepare_list.append(e)

    if len(prepare_list)<1 and anime.tmdb["season"]["episodes"][-1]["episode_number"] in local_episodes:
        logger.info(anime.name+"已完结")
        db_util.delete_follow(anime.tm_id)

    return prepare_list


def get_bangumi_search_tags(anime):
    """
    获取萌番组搜索时用的tags
    :param anime:
    :return:
    """
    tags = []
    if anime.bangumi_tag:
        tags.append(anime.bangumi_tag)
        if anime.language:
            tags.append(parse_bangumi_tag(anime.language))
        if anime.team:
            tags.append(anime.team)
    else:
        data = json.dumps({
            "name": anime.name,
            "keyword": True,
            "multi": True
        })
        res = requests.post(bangumiTagSearch, data=data, headers=request_head)
        res = json.loads(res.content)
        if res['success'] and res["found"]:
            tags.append(res['tag'][0]['_id'])
            if anime.language:
                tags.append(parse_bangumi_tag(anime.language))
            if anime.team:
                tags.append(anime.team)

    return tags


def bangumi_search(tag_id, episode: int, page: int = 1):
    """
    从萌番组搜索目标种子
    默认按种子数排序

    :param tag_id:  搜索参数
    :param episode: 查找集数
    :param page:    多页搜索用的页数
    :return:
    """
    result = {"magnet": None, "torrent": None}
    data = json.dumps({
        "tag_id": tag_id,
        "p": page
    })
    time.sleep(1)
    res = json.loads(requests.post(bangumiSearch, data=data, headers=request_head).content)
    torrents = res["torrents"]

    def get_seeders(o):
        return o["seeders"]

    torrents.sort(key=get_seeders, reverse=True)
    for t in res["torrents"]:
        if re.search("(\[{}\])|(【{}】)|(\({}\))|(第{}集)|(\[{}\ ?[Vv]2\])|(【{}\ ?[Vv]2】)|(\ {}\ )".replace("{}", parse_num(
                episode)), t["title"]):
            if t["magnet"]:
                # 磁力链
                result["magnet"] = t["magnet"]
            result["torrent"] = "https://bangumi.moe/download/torrent/" + t["_id"] + "/" + t["title"].replace("/",
                                                                                                              "_") + ".torrent"
        if result["torrent"] is not None:
            break

    try:
        if result["torrent"] is None and page < res["page_count"]:
            return bangumi_search(tag_id, episode, page + 1)
    except KeyError:
        pass
    return result


def get_bangumi_download_link(anime):
    """
    获取萌番组下载链接

    :param anime:
    :return:
    """
    search_tags = get_bangumi_search_tags(anime)
    if len(search_tags) < 1:
        logger.info("Bangumi未搜索到" + anime.name + "标签")
        return
    res = bangumi_search(tag_id=search_tags, episode=anime.episode)
    if res["torrent"] is None:
        logger.info("Bangumi未搜索到{}".format(anime.format_name))
        return
    if res["magnet"] is not None:
        # 磁力链
        anime.set_magnet(res["magnet"])
        logger.info("找到{}磁力链接".format(anime.name))
    # 种子文件链接
    anime.set_torrent(res["torrent"])


def get_download_link(anime):
    """
    获取下载链接

    :param anime:
    :return:
    """
    get_bangumi_download_link(anime)


client = None


def download(anime, semaphore):
    # 线程加锁
    semaphore.acquire()
    try:
        global client
        if client is None:
            client = aria2rpc.aria2_rpc_api(host=ARIAURL, secret=ARIAID)
        option = {
            "dir": anime.path,
            "out": anime.format_name
        }
        uris = []
        if anime.magnet:
            uris.append(anime.magnet)
        else:
            uris.append(anime.torrent_url)
        if len(uris) > 0:
            time.sleep(int(random() * 10))
            res = client.addUri(uris=uris, options=option)
            logger.info("开始下载" + option['out'])
            logger.info("下载ID为：" + res)
            anime.downloading(client, res)
        else:
            logger.info("未找到" + option['out'])
    except ConnectionError:
        logger.debug("网络异常，可能是未启动aria2")
    finally:
        # 释放线程
        semaphore.release()


def main():
    global db_util
    try:
        db_util = DBUtil()
        logger.add(LOG_FILE)
        semaphore = threading.BoundedSemaphore(THREAD_NUM)
        follow_list = get_follow_list()
        for anime in follow_list:
            logger.info("开始查找" + anime.name)
            prepare_list = get_tmdb_data(anime)
            if len(prepare_list) < 1:
                logger.info(anime.name + "查找完成，没有未下载剧集")
            for p_anime in prepare_list:
                get_download_link(p_anime)
                if p_anime.magnet:
                    t = threading.Thread(target=download, args=(p_anime, semaphore))
                    t.start()
    except KeyboardInterrupt:
        logger.info("程序已停止")
    except ConnectionError:
        logger.debug("网络异常，5秒后重试")
        time.sleep(5)
        main()
    finally:
        db_util.close()


if __name__ == '__main__':
    main()
