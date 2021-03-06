import arrow
import re
import feedparser
import json
import time

from hbconfig import Config
from sklearn import tree

from .pocket import Pocket

from ..slack.resource import MsgResource
from ..slack.slackbot import SlackerAdapter
from ..slack.template import MsgTemplate

from ..utils.data_handler import DataHandler
from ..utils.data_loader import FeedData
from ..utils.data_loader import FeedDataLoader
from ..utils.logger import Logger
from ..utils.logger import DataLogger


class FeedNotifier:

    MAX_KEEP = 40

    def __init__(self, slackbot: SlackerAdapter = None) -> None:
        self.logger = Logger().get_logger()
        self.feed_logger = DataLogger("feed").get_logger()

        self.data_handler = DataHandler()
        self.feeds = self.data_handler.read_feeds()
        self.feed_classifier = None
        if Config.bot.get("FEED_CLASSIFIER", False):
            self.feed_classifier = FeedClassifier()

        if slackbot is None:
            self.slackbot = SlackerAdapter(
                channel=Config.slack.channel.get("FEED", "#general")
            )
        else:
            self.slackbot = slackbot

    def notify_all(self) -> None:
        self.logger.info("Check feed_list")
        for category, feeds in self.feeds.items():
            for feed in feeds:
                try:
                    results = self.get_notify_list(category, feed)
                    self.notify(category, feed, results)
                except Exception as e:
                    self.logger.error(f"FEED Error: {e}")
                    self.logger.exception("feed")

    def get_notify_list(self, category: str, feed: tuple) -> list:
        CACHE_FILE_NAME = "cache_feed.json"
        cache_data = self.data_handler.read_cache(fname=CACHE_FILE_NAME)

        feed_name, feed_url, save_pocket = feed
        f = feedparser.parse(feed_url)

        def get_timestamp(x):
            update_time = x.get("updated_parsed", arrow.now().timestamp)
            if type(update_time) == time.struct_time:
                update_time = time.mktime(update_time)
            if update_time is None:
                return arrow.now().timestamp
            return update_time

        f.entries = sorted(
            f.entries, key=lambda x: get_timestamp(x), reverse=True
        )

        # get Latest Feed
        noti_list = []
        if feed_url in cache_data:
            previous_update_date = arrow.get(cache_data[feed_url])
            for e in f.entries:
                if getattr(e, "updated_parsed", None):
                    e_updated_date = arrow.get(e.updated_parsed)
                else:
                    e_updated_date = arrow.now()

                if e_updated_date > previous_update_date:
                    noti_list.append(self.__make_entry_tuple(category, e, feed_name))

        elif f.entries:
            e = f.entries[0]
            noti_list.append(self.__make_entry_tuple(category, e, feed_name))
        else:
            pass

        if f.entries:
            last_e = f.entries[0]
            last_updated_date = arrow.get(last_e.get("updated_parsed", None))
            self.data_handler.edit_cache((feed_url, str(last_updated_date)), fname=CACHE_FILE_NAME)

        # filter feeded entry link
        cache_entry_links = set(cache_data.get("feed_links", []))
        noti_list = list(filter(lambda e: e[1] not in cache_entry_links, noti_list))

        # Cache entry link
        for entry in noti_list:
            _, entry_link, _ = entry
            cache_entry_links.add(entry_link)

        self.data_handler.edit_cache(
            ("feed_links", list(cache_entry_links)[-self.MAX_KEEP :]), fname=CACHE_FILE_NAME
        )

        if len(cache_data) == 0:  # cache_data is Empty. (Error)
            return []

        # Append 'save_pocket' flags
        noti_list = [(link, save_pocket) for link in noti_list]
        return noti_list

    def __make_entry_tuple(self, category: str, entry: dict, feed_name: str) -> tuple:
        entry_title = f"[{category}] - {feed_name} \n" + entry.get("title", "")
        entry_link = entry.get("link", "")
        entry_description = f"Link : {entry_link} \n" + self.__remove_tag(
            entry.get("description", ""), entry_link
        )
        return (entry_title, entry_link, entry_description)

    def __remove_tag(self, text: str, entry_link: str) -> str:
        text = re.sub("<.+?>", "", text, 0, re.I | re.S)
        text = re.sub("&nbsp;|\t|\r|", "", text)
        text = re.sub(entry_link, "", text)
        return text

    def notify(self, category: str, feed: tuple, results: list):
        if len(results) == 0:
            feed_name = feed[0]
            self.slackbot.send_message(text=MsgResource.FEED_NO_NEW_POST(feed_name=feed_name))
            return

        for (parsed_feed, save_pocket) in results:
            feed_header = parsed_feed[0].split("\n")

            category = feed_header[0]
            title = feed_header[1]
            link = parsed_feed[1]

            # Depense
            if not link.startswith("http"):
                continue

            self.feed_logger.info(json.dumps({"category": category, "title": title}))

            if self.feed_classifier is not None and self.feed_classifier.predict(
                link, category, force=save_pocket
            ):
                self.slackbot.send_message(
                    text=MsgResource.PREDICT_FEED_TRUE(title=category + ": " + title)
                )
                continue

            attachments = MsgTemplate.make_feed_template(parsed_feed)
            self.slackbot.send_message(attachments=attachments)


class FeedClassifier:
    def __init__(self):
        self.logger = Logger().get_logger()

        train_X = FeedData().train_X
        train_y = FeedData().train_y
        self.category_ids = FeedData().category_ids

        self.clf = tree.DecisionTreeClassifier()
        self.clf = self.clf.fit(train_X, train_y)

    def predict(self, link, category, force=False):
        category_id = self.category_ids.get(category.strip(), None)
        if category_id is None:
            return False

        if force is True:
            save_result = self.save_to_pocket(category, link)
            return save_result

        return False

        # predict_result = self.clf.predict(category_id)[0]
        # if predict_result == FeedDataLoader.TRUE_LABEL:
            # self.logger.info("predict result is True, Save feed to Pocket ...")
            # save_result = self.save_to_pocket(category, link)
            # return save_result
        # else:
            # return False

    def save_to_pocket(self, category, link):
        try:
            pocket = Pocket()
            tags = self.extract_tags(category)
            pocket.add(link, tags=tags)
            return True
        except BaseException as e:
            self.logger.exception(e)
            return False

    def extract_tags(self, tags):
        tags = tags.strip()
        tags = tags.replace("[", "")
        tags = tags.replace("]", "")
        tags = tags.split(" - ")
        return tags
