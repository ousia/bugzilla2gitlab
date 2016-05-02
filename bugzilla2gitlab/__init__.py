from xml.etree import ElementTree
import os.path
import re

from config import Config
from helpers import _perform_request, markdown_table_row, format_datetime

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, "config")
config = Config(CONFIG_PATH)

class MigrationClient(object):

    def migrate(self, bug_list, dry_run=False):
        for bug in bug_list:
            print("Migrating bug {}".format(bug))
            self.migrate_one(bug, dry_run=dry_run)

    def migrate_one(self, bugzilla_bug_id, dry_run=False):
        fields = self.get_bugzilla_bug(bugzilla_bug_id)
        it = IssueThread(fields, dry_run=dry_run)
        it.save()

    def get_bugzilla_bug(self, bid):
        url = "{}show_bug.cgi?ctype=xml&id={}".format(config.bugzilla_base_url, bid)
        response = _perform_request(url, "get", paginated=False, json=False)
        tree = ElementTree.fromstring(response.content)
        bug_fields = {
            "long_desc" : [],
            "attachment": [],
            "cc": [],
        }
        for bug in tree:
            for fields in bug:
                if fields.tag in ("long_desc", "attachment"):
                    new = {}
                    for data in fields:
                        new[data.tag] = data.text
                    bug_fields[fields.tag].append(new)
                elif fields.tag == "cc":
                    bug_fields[fields.tag].append(fields.text)
                else:
                    bug_fields[fields.tag] = fields.text

        return bug_fields


class IssueThread(object):

    def __init__(self, fields, dry_run=False):
        self.dry_run = dry_run
        self.load_objects(fields)

    def load_objects(self, fields):
        self.issue = Issue(fields, dry_run=self.dry_run)
        self.comments = []
        for c in fields["long_desc"]:
            if c.get("thetext"):
                self.comments.append(Comment(c, dry_run=self.dry_run))

    def save(self):
        self.issue.save()

        for comment in self.comments:
            comment.issue_id = self.issue.id
            comment.save()

        if self.issue.status in config.closed_statuses:
            self.issue.close()


class Issue(object):
    required_fields = ["sudo", "title", "description", "status"]
    data_fields = ["sudo", "title", "description", "status", "assignee_id", "milestone",
                   "labels"]

    def __init__(self, bugzilla_fields, dry_run=False):
        self.headers = config.headers
        self.dry_run = dry_run
        self.load_fields(bugzilla_fields)

    def load_fields(self, fields):
        self.title = fields["short_desc"]
        self.sudo = config.gitlab_users[config.bugzilla_users[fields["reporter"]]]
        self.assignee_id = config.gitlab_users[config.bugzilla_users[fields["assigned_to"]]]
        self.status = fields["bug_status"]
        self.create_labels(fields["component"], fields.get("op_sys"))
        self.create_description(fields)

    def create_labels(self, component, operating_system):
        labels = []
        labels.append("bugzilla")

        component_label = config.component_mappings.get(component)
        if component_label:
            labels.append(component_label)

        if operating_system and operating_system != "Other":
            labels.append(operating_system)

        self.labels = ",".join(labels)

    def create_description(self, fields):
        ext_description = ""

        # markdown table header
        self.description = markdown_table_row("", "")
        self.description += markdown_table_row("---", "---")

        bug_id = fields.pop("bug_id")
        link = "{}show_bug.cgi?id={}".format(config.bugzilla_base_url, bug_id)
        self.description += markdown_table_row("Bugzilla Link",
                                               "[{}]({})".format(bug_id, link))

        formatted_dt = format_datetime(fields.pop("creation_ts"), "%b %d, %Y %H:%M")
        self.description += markdown_table_row("Created on", formatted_dt)

        if fields.get("resolution"):
            self.description += markdown_table_row("Resolution", fields.pop("resolution"))
            self.description += markdown_table_row("Resolved on",
                                                   format_datetime(fields.pop("delta_ts"),
                                                                   "%b %d, %Y %H:%M"))

        self.description += markdown_table_row("Version", fields.pop("version"))
        self.description += markdown_table_row("OS", fields.pop("op_sys"))
        self.description += markdown_table_row("Architecture", fields.pop("rep_platform"))

        # add first comment to the issue description
        if fields["reporter"] == fields["long_desc"][0]["who"] and fields["long_desc"][0]["thetext"]:
            ext_description += "\n## Extended Description \n"
            ext_description += "\n\n".join(re.split("\n*", fields["long_desc"][0]["thetext"]))
            del fields["long_desc"][0]

        attachments = []
        to_delete = []
        for i in range(0, len(fields["long_desc"])):
            comment = fields["long_desc"][i]
            # any attachments from the reporter in comments should also go in the issue description
            if comment.get("attachid") and comment.get("who") == fields["reporter"]:
                filename = Attachment.parse_filename(comment.get("thetext"))
                attachment_markdown = Attachment(comment.get("attachid"), filename,
                                                 dry_run=self.dry_run).save()
                attachments.append(attachment_markdown)
                to_delete.append(i)

        # delete comments that have already added to the issue description
        for i in reversed(to_delete):
            del fields["long_desc"][i]

        if attachments:
            self.description += markdown_table_row("Attachments", ", ".join(attachments))

        if ext_description:
            if fields["reporter"] == "scipweb":
                # try to get reporter email from the body
                description, part, user_data = ext_description.rpartition("Submitter was ")
                # partition found matching string
                if part:
                    regex = r"^(\S*)\s?.*$"
                    email = re.match(regex, user_data, flags=re.M).group(1)
                    self.description += markdown_table_row("Reporter", email)
            elif config.bugzilla_users[fields["reporter"]] == "ghost":
                self.description += markdown_table_row("Reporter", fields["reporter"])

            self.description += ext_description


    def validate(self):
        for field in self.required_fields:
            value = getattr(self, field)
            if not value:
                return False
        return True

    def save(self, dry_run=False):
        if not self.validate():
            raise Exception("Validation error")
        url = "{}/projects/{}/issues".format(config.gitlab_base_url, config.gitlab_project_id)
        data = {k:v for k,v in self.__dict__.iteritems() if k in self.data_fields}
        self.headers["sudo"] = self.sudo

        if self.dry_run:
            print url, "dry_run"
            self.id = 5
            return

        response = _perform_request(url, "post", headers=self.headers, data=data, json=True, paginated=False)
        self.id = response["id"]


    def close(self):
        url = "{}/projects/{}/issues/{}".format(config.gitlab_base_url, config.gitlab_project_id, self.id)
        data = {
            "state_event" : "close",
        }
        self.headers["sudo"] = self.sudo

        if self.dry_run:
            print url, "dry_run"
            return

        _perform_request(url, "put", headers=self.headers, data=data)

class Comment(object):

    required_fields = ["sudo", "body", "issue_id"]
    data_fields = ["body"]

    def __init__(self, bugzilla_fields, dry_run=False):
        self.headers = config.headers
        self.dry_run = dry_run
        self.load_fields(bugzilla_fields)

    def load_fields(self, c):
        self.sudo = config.gitlab_users[config.bugzilla_users[c["who"]]]
        if config.bugzilla_users[c["who"]] == "ghost":
            self.body = "By {} on {}\n\n".format(c["who"],
                                                 format_datetime(c.pop("bug_when"),
                                                                 "%b %d, %Y %H:%M"))
        else:
            self.body = format_datetime(c.pop("bug_when"), "%b %d, %Y %H:%M") + "\n\n"
        if c.get("attachid"):
            filename = Attachment.parse_filename(c.get("thetext"))
            attachment_markdown = Attachment(c.get("attachid"), filename, self.dry_run).save()
            self.body += attachment_markdown
        else:
            self.body += c.pop("thetext")


    def validate(self):
        for field in self.required_fields:
            value = getattr(self, field)
            if not value:
                print field
                return False
        return True

    def save(self):
        if not self.validate():
            raise Exception("Validation error")

        self.headers["sudo"] = self.sudo
        url = "{}/projects/{}/issues/{}/notes".format(config.gitlab_base_url, config.gitlab_project_id, self.issue_id)
        data = {k:v for k,v in self.__dict__.iteritems() if k in self.data_fields}

        if self.dry_run:
            print url, "dry_run"
            return

        _perform_request(url, "post", headers=self.headers, data=data, json=True, paginated=False)


class Attachment(object):
    def __init__(self, bugzilla_attachment_id, filename, dry_run=False):
        self.dry_run = dry_run
        self.id = bugzilla_attachment_id
        self.filename = filename
        self.headers = config.headers

    @classmethod
    def parse_filename(cls, comment):
        regex = "^Created attachment (\d*)\s?(.*)$"
        matches = re.match(regex, comment, flags=re.M)
        if not matches:
            raise Exception("Failed to match comment string: {}".format(comment))
        return matches.group(2)

    def save(self):
        url = "{}attachment.cgi?id={}".format(config.bugzilla_base_url, self.id)
        result = _perform_request(url, "get", json=False, paginated=False)

        url = "{}/projects/{}/uploads".format(config.gitlab_base_url, config.gitlab_project_id)
        f = {"file": (self.filename, result.content)}

        if self.dry_run:
            print url, "dry_run"
            return "[attachment]({})".format(self.filename)

        attachment = _perform_request(url, "post", headers=self.headers, files=f, json=True,
                                      paginated=False)
        return attachment["markdown"]
