#!/usr/bin/python
# -*- coding: utf-8 -*-

###########################################################################
# Ava's Global speedrunning leaderboard
# Copyright (C) 2017 Samuel Therrien
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Contact:
# samuel.06@hotmail.com
###########################################################################

import math
import time
import traceback
from collections import Counter
from sys import stdout
from threading import Thread

import gspread
import httplib2
import oauth2client
import requests

from CONSTANTS import *


def print(string):
    stdout.write(str(string) + "\n")


class UserUpdaterError(Exception):
    """ raise UserUpdaterError({"error":"On Status Label", "details":"Details of error"}) """
    pass


class SpeedrunComError(UserUpdaterError):
    """ raise NotFoundError({"error":"404 Not Found", "details":"Details of error"}) """
    pass


class Run():
    id_ = ""
    primary_t = 0.0
    game = ""
    category = ""
    variables = {}
    level = ""
    level_count = 0
    _points = 0

    def __init__(self, id_, primary_t, game, category, variables={}, level=""):
        self.id_ = id_
        self.primary_t = primary_t
        self.game = game
        self.category = category
        self.variables = variables
        self.level = level
        self.__set_points()

    def __str__(self):
        level_str = "Level/{}: {}, ".format( self.level_count, self.level) if self.level else ""
        return "Run: <Game: {}, Category: {}, {}{} {}>".format(self.game, self.category, level_str, self.variables, math.ceil(self._points * 100) / 100)

    def compare_str(self):
        return "{}-{}".format(self.category, self.level)

    def __set_points(self):
        self._points = 0
        # If the run is an Individual Level, adapt the request url
        lvl_cat_str = "level/{level}/".format(level=self.level) if self.level else "category/"
        url = "https://www.speedrun.com/api/v1/leaderboards/{game}/" \
              "{lvl_cat_str}{category}?video-only=true&embed=players".format(game=self.game, lvl_cat_str=lvl_cat_str, category=self.category)
        for var_id, var_value in self.variables.items():
            url += "&var-{id}={value}".format(id=var_id, value=var_value)
        leaderboard = get_file(url)

        if len(leaderboard["data"]["runs"]) >= MIN_LEADERBOARD_SIZE:  # Check to avoid useless computation
            previous_time = leaderboard["data"]["runs"][0]["run"]["times"]["primary_t"]
            is_speedrun = False

            # Get a list of all banned players in this leaderboard
            banned_players = []
            for player in leaderboard["data"]["players"]["data"]:
                if player.get("role") == "banned":
                    banned_players.append(player["id"])

            mean = 0.0
            sigma = 0.0
            population = 0
            value = 0.0
            for run in leaderboard["data"]["runs"]:
                value = run["run"]["times"]["primary_t"]

                # Making sure this is a speedrun and not a score leaderboard
                if not is_speedrun:  # To avoid false negatives due to missing primary times, stop comparing once we know it's a speedrun
                    if value < previous_time:
                        break  # Score based leaderboard. No need to keep looking
                    elif value > previous_time:
                        is_speedrun = True

                # Updating leaderboard size and rank
                if run["place"] > 0:
                    for player in run["run"]["players"]:
                        if player.get("id") in banned_players: break
                    else:
                        # If no participant is banned and the run is valid
                        population += 1
                        mean_temp = mean
                        mean += (value - mean_temp) / population
                        sigma += (value - mean_temp) * (value - mean)

            if is_speedrun:  # Check to avoid useless computation
                standard_deviation = (sigma / population) ** 0.5
                if standard_deviation > 0:  # All runs must not have the exact same time
                    signed_deviation = mean - self.primary_t
                    lowest_deviation = value - mean
                    adjusted_deviation = signed_deviation+lowest_deviation
                    adjusted_standard_deviation = standard_deviation+lowest_deviation
                    if adjusted_deviation > 0:  # The last run isn't worth any points TODO: detect if a run is the last earlier in the code
                        normalized_signed_deviation = adjusted_deviation/adjusted_standard_deviation
                        self._points = (normalized_signed_deviation ** DEVIATION_MULTIPLIER) * 10

                        # If the run is an Individual Level and worth looking at, set the level count
                        if self.level and self._points > 0:
                            url = "https://www.speedrun.com/api/v1/games/{game}/levels".format(game=self.game)
                            levels = get_file(url)
                            self.level_count = len(levels["data"])
                            self._points /= self.level_count + 1
        print(self)


class User:
    _points = 0
    _name = ""
    _weblink = ""
    _id = ""
    _banned = False
    _point_distribution_str = ""

    def __init__(self, id_or_name: str) -> None:
        self._id = id_or_name
        self._name = id_or_name

    def __str__(self) -> str:
        return "User: <{}, {}, {}{}>".format(self._name, math.ceil(self._points * 100) / 100, self._id, "(Banned)" if self._banned else "")

    def set_code_and_name(self) -> None:
        url = "https://www.speedrun.com/api/v1/users/{user}".format(user=self._id)
        try:
            infos = get_file(url)
        except SpeedrunComError as exception:
            raise UserUpdaterError({"error": exception.args[0]["error"],
                                    "details": "User \"{}\" not found.\n"
                                               "Make sure the name or ID is typed properly. It's possible the user you're looking for changed its name. "
                                               "In case of doubt, use its ID.".format(self._id)})
        self._id = infos["data"]["id"]
        self._weblink = infos["data"]["weblink"]
        self._name = infos["data"]["names"].get("international")
        japanese_name = infos["data"]["names"].get("japanese")
        if japanese_name: self._name += " ({})".format(japanese_name)
        if infos["data"]["role"] == "banned":
            self._banned = True
            self._points = 0

    def set_points(self) -> None:
        counted_runs = {}

        def set_points_thread(pb):
            try:
                # Check if it's a valid run (has a category AND has video verification)
                if pb["run"]["category"] and pb["run"].get("videos"):
                    # Get a list of the game's subcategory variables
                    url = "https://www.speedrun.com/api/v1/games/{game}/variables".format(game=pb["run"]["game"])
                    game_variables = get_file(url)
                    game_subcategory_ids = []
                    for game_variable in game_variables["data"]:
                        if game_variable["is-subcategory"]:
                            game_subcategory_ids.append(game_variable["id"])

                    pb_subcategory_variables = {}
                    # For every variable in the run...
                    for pb_var_id, pb_var_value in pb["run"]["values"].items():
                        # ...find if said variable is one of the game's subcategories...
                        if pb_var_id in game_subcategory_ids:
                            # ... and add it to the run's subcategory variables
                            pb_subcategory_variables[pb_var_id] = pb_var_value

                    run = Run(pb["run"]["id"], pb["run"]["times"]["primary_t"], pb["run"]["game"], pb["run"]["category"], pb_subcategory_variables, pb["run"]["level"])
                    # If a category has already been counted, only keep the one that's worth the most.
                    # This can happen in leaderboards with multiple coop runs or multiple subcategories.
                    if run._points > 0:
                        if run.compare_str() in counted_runs:
                            counted_runs[run.compare_str()] = max(counted_runs[run.compare_str()], run._points)
                        else:
                            counted_runs[run.compare_str()] = run._points
            except UserUpdaterError as exception:
                threadsException.append(exception.args[0])
            except Exception:
                threadsException.append({"error": "Unhandled", "details": traceback.format_exc()})
            finally:
                update_progress(1, 0)

        if not self._banned:
            url = "https://www.speedrun.com/api/v1/users/{user}/personal-bests".format(user=self._id) #.format(user=self._id)
            pbs = get_file(url)
            self._points = 0
            update_progress(0, len(pbs["data"]))
            threads = []
            for pb in pbs["data"]:
                threads.append(Thread(target=set_points_thread, args=(pb,)))
            for t in threads: t.start()
            for t in threads: t.join()
            # Sum up the runs' score
            self._point_distribution_str = "\nCategory-Level    | Points\n----------------- | ------".format(self._name)
            for category_level, points in counted_runs.items():
                self._points += points
                self._point_distribution_str += "\n{0:<17} | {1}".format(category_level, math.ceil(points * 100) / 100)
            if self._banned or self._points < 1:
                self._points = 0  # In case the banned flag has been set mid-thread or the user doesn't have at least 1 point
        else:
            self._points = 0
        update_progress(1, 0)


session = requests.Session()


def get_file(p_url: str) -> dict:
    """
    Returns the content of "url" parsed as JSON dict.

    Parameters
    ----------
    p_url : str   # The url to query
    """
    global session
    print(p_url)  # debugstr
    while True:
        try:
            rawdata = session.get(p_url)
        except requests.exceptions.ConnectionError as exception:
            raise UserUpdaterError({"error": "Can't establish connexion to speedrun.com", "details": exception})
        try:
            jsondata = rawdata.json()
            if type(jsondata) != dict: print("{}:{}".format(type(jsondata), jsondata))  # debugstr
            if "status" in jsondata: raise SpeedrunComError({"error": "{} (speedrun.com)".format(jsondata["status"]), "details": jsondata["message"]})
            #"details": "User \"{}\" not found. Make sure the name or ID is typed properly. "
            #                                   "It's possible the user you're looking for changed its name. In case of doubt, use its ID.".format(self._id)})
            rawdata.raise_for_status()
            break
        except requests.exceptions.HTTPError as exception:
            if rawdata.status_code in HTTP_RETRYABLE_ERRORS:
                print("WARNING: {}. Retrying in {} seconds.".format(exception.args[0], HTTPERROR_RETRY_DELAY))  # debugstr
                time.sleep(HTTPERROR_RETRY_DELAY)
            else:
                raise UserUpdaterError({"error": "HTTPError {}".format(rawdata.status_code), "details": exception.args[0]})
    return (jsondata)


def update_progress(p_current: int, p_max: int) -> None:
    global statusLabel_current
    global statusLabel_max
    statusLabel_current += p_current
    statusLabel_max += p_max
    percent = int(statusLabel_current / statusLabel_max * 100) if statusLabel_max > 0 else 0
    statusLabel.configure(text="Fetching online data from speedrun.com. Please wait... [{}%] ({}/{})".format(percent, statusLabel_current, statusLabel_max))


worksheet = None
gs_client = None


def get_updated_user(p_user_id: str, p_statusLabel: object) -> str:
    """Called from ui.update_user_thread() and AutoUpdateUsers.run()"""
    global statusLabel
    statusLabel = p_statusLabel
    global statusLabel_current
    statusLabel_current = 0
    global statusLabel_max
    statusLabel_max = 0
    global session
    global worksheet
    global gs_client
    global threadsException
    threadsException = []
    text_output = p_user_id

    try:
        # Send to Web App
        def send_to_webapp(p_user_name_or_id: str) -> None:
            session.post("https://avasam.pythonanywhere.com/", data={"action": "update-user", "name-or-id": p_user_name_or_id})

        Thread(target=send_to_webapp, args=(p_user_id,)).start()

        # Check if already connected
        if not (gs_client and worksheet):
            # Authentify to Google Sheets API
            statusLabel.configure(text="Establishing connexion to online Spreadsheet...")
            gs_client = gspread.authorize(credentials)
            print("https://docs.google.com/spreadsheets/d/{spreadsheet}\n".format(spreadsheet=SPREADSHEET_ID))
            worksheet = gs_client.open_by_key(SPREADSHEET_ID).sheet1

        # Refresh credentials
        gs_client.login()
        statusLabel.configure(text="Fetching online data from speedrun.com. Please wait...")
        user = User(p_user_id)
        print("{}\n{}".format(SEPARATOR, user._name))  # debugstr

        update_progress(0, 2)
        user.set_code_and_name()
        user.set_points()
        update_progress(1, 0)  # Because user.set_code_and_name() is too fast

        if threadsException == []:
            if user._points > 0:  # TODO: once the database is full, move this in "# If user not found, add a row to the spreadsheet" (user should also be removed from spreadsheet)
                statusLabel.configure(text="Updating the leaderboard...")
                print("\nLooking for {}".format(user._id))  # debugstr

                # Try and find the user by its id_
                worksheet = gs_client.open_by_key(SPREADSHEET_ID).sheet1
                row = 0
                # As of 2017/07/16 with current code searching by range is faster than col_values most of the time
                # t1 = time.time()
                row_count = worksheet.row_count
                print("slow call to GSheets")
                cell_list = worksheet.range(ROW_FIRST, COL_USERID, row_count, COL_USERID)
                print("slow call done")
                for cell in cell_list:
                    if cell.value == user._id:
                        row = cell.row
                        break
                # t2 = time.time()
                # row_count = 0
                # cell_values_list = worksheet.col_values(COL_USERID)
                # for value in cell_values_list:
                #    row_count += 1
                #    if value == user._id: row = row_count
                # t3 = time.time()
                # print("range took    : {} seconds\ncol_values took: {} seconds".format(t2-t1, t3-t2))
                # print("worksheet.range itself took {} seconds".format(tr2-tr1))
                timestamp = time.strftime("%Y/%m/%d %H:%M")
                linked_name = "=HYPERLINK(\"{}\";\"{}\")".format(user._weblink, user._name)
                if row >= ROW_FIRST:
                    text_output = "{} found. Updated its cell.".format(user)
                    cell_list = worksheet.range(row, COL_USERNAME, row, COL_LAST_UPDATE)
                    cell_list[0].value = linked_name
                    cell_list[1].value = user._points
                    cell_list[2].value = timestamp
                    worksheet.update_cells(cell_list)
                # If user not found, add a row to the spreadsheet
                else:
                    text_output = "{} not found. Added a new row.".format(user)
                    values = ["=IF($C{1}=$C{0};$A{0};ROW()-3)".format(row_count, row_count + 1),
                              linked_name,
                              user._points,
                              timestamp,
                              user._id]
                    worksheet.insert_row(values, index=row_count + 1)
                text_output += user._point_distribution_str
            else:
                text_output = "Not updloading data as {} {}.".format(user, "is banned" if user._banned else "has a score of 0")

        else:
            error_str_list = []
            for e in threadsException: error_str_list.append("Error: {}\n{}".format(e["error"], e["details"]))
            error_str_counter = Counter(error_str_list)
            errors_str = "{0}\nhttps://github.com/Avasam/Global_speedrunning_leaderboard/issues\nNot updloading data as some errors were caught during execution:\n{0}\n".format(SEPARATOR)
            for error, count in error_str_counter.items(): errors_str += "[x{}] {}\n".format(count, error)
            text_output += ("\n" if text_output else "") + errors_str

        print(text_output)
        statusLabel.configure(text="Done! " + ("({} error".format(len(threadsException)) +
                                               ("s" if len(threadsException) > 1 else "") + ")" if threadsException != [] else ""))
        return (text_output)

    except httplib2.ServerNotFoundError as exception:
        raise UserUpdaterError({"error": "Server not found",
                                "details": "{}\nPlease make sure you have an active internet connection".format(
                                    exception)})
    except (requests.exceptions.ChunkedEncodingError, ConnectionAbortedError) as exception:
        raise UserUpdaterError({"error": "Connexion interrupted", "details": exception})
    except gspread.exceptions.SpreadsheetNotFound:
        raise UserUpdaterError({"error": "Spreadsheet not found",
                                "details": "https://docs.google.com/spreadsheets/d/{spreadsheet}".format(
                                    spreadsheet=SPREADSHEET_ID)})
    except requests.exceptions.ConnectionError as exception:
        raise UserUpdaterError({"error": "Can't connect to Google Sheets", "details": exception})
    except oauth2client.client.HttpAccessTokenRefreshError as exception:
        raise UserUpdaterError({"error": "Authorization problems",
                                "details": "{}\nThis version of the app may be outdated. "
                                           "Please see https://github.com/Avasam/Global_speedrunning_leaderboard/releases".format(exception)})


# !Autoupdater
class AutoUpdateUsers(Thread):
    BASE_URL = "https://www.speedrun.com/api/v1/users?orderby=signup&max=200&offset={}".format(AUTOUPDATER_OFFSET)
    paused = True
    global statusLabel

    def __init__(self, p_statusLabel, **kwargs):
        Thread.__init__(self, **kwargs)
        self.statusLabel = p_statusLabel

    def run(self):
        def auto_updater_thread(user):
            while True:
                self.__check_for_pause()
                try:
                    try:
                        get_updated_user(user["id"], self.statusLabel)
                        break
                    except gspread.exceptions.RequestError as exception:
                        if exception.args[0] in HTTP_RETRYABLE_ERRORS:
                            print("WARNING: {}. Retrying in {} seconds.".format(exception.args[0], HTTPERROR_RETRY_DELAY))  # debugstr
                            time.sleep(HTTPERROR_RETRY_DELAY)
                        else:
                            raise UserUpdaterError(
                                {"error": "Unhandled RequestError", "details": traceback.format_exc()})
                    except Exception:
                        raise UserUpdaterError({"error": "Unhandled", "details": traceback.format_exc()})
                except UserUpdaterError as exception:
                    print("WARNING: Skipping user {}. {}".format(user["id"], exception.args[0]["details"]))  # debugstr
                    break

        url = self.BASE_URL
        while True:
            self.__check_for_pause()
            self.statusLabel.configure(text="Auto-updating userbase...")
            users = get_file(url)
            # threads = []
            for user in users["data"]:
                auto_updater_thread(user)  # Not threaded
            # threads.append(Thread(target=auto_updater_thread, args=(user,)))
            # for t in threads: t.start()
            # for t in threads: t.join()

            link_found = False
            for link in users["pagination"]["links"]:
                if link["rel"] == "next":
                    url = link["uri"]
                    link_found = True
            if not link_found: url = self.BASE_URL

    def __check_for_pause(self):
        while self.paused:
            pass
