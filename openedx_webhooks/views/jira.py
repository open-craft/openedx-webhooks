# coding=utf-8
"""
These are the views that process webhook events coming from JIRA.
"""

from __future__ import unicode_literals, print_function

import sys
import json
import re
from collections import defaultdict

import bugsnag
import requests
from urlobject import URLObject
from flask import request, render_template, make_response, jsonify
from flask_dance.contrib.jira import jira
from flask_dance.contrib.github import github
from openedx_webhooks import app
from openedx_webhooks.oauth import jira_get
from openedx_webhooks.utils import (
    pop_dict_id, memoize, jira_paginated_get, to_unicode,
    jira_users, jira_group_members
)


@memoize
def get_jira_custom_fields():
    """
    Return a name-to-id mapping for the custom fields on JIRA.
    """
    field_resp = jira.get("/rest/api/2/field")
    if not field_resp.ok:
        raise requests.exceptions.RequestException(field_resp.text)
    field_map = dict(pop_dict_id(f) for f in field_resp.json())
    return {
        value["name"]: id
        for id, value in field_map.items()
        if value["custom"]
    }


@memoize
def get_jira_issue(key):
    return jira_get("/rest/api/2/issue/{key}".format(key=key))


@app.route("/jira/issue/rescan", methods=("GET", "POST"))
def jira_rescan_issues():
    if request.method == "GET":
        # just render the form
        return render_template("jira_rescan_issues.html")
    jql = request.form.get("jql") or 'status = "Needs Triage" ORDER BY key'
    bugsnag_context = {"jql": jql}
    bugsnag.configure_request(meta_data=bugsnag_context)
    issues = jira_paginated_get(
        "/rest/api/2/search", jql=jql, obj_name="issues", session=jira,
    )
    results = {}

    for issue in issues:
        issue_key = to_unicode(issue["key"])
        results[issue_key] = issue_opened(issue)

    resp = make_response(json.dumps(results), 200)
    resp.headers["Content-Type"] = "application/json"
    return resp


@app.route("/jira/issue/created", methods=("POST",))
def jira_issue_created():
    """
    Received an "issue created" event from JIRA. See `JIRA's webhook docs`_.

    Ideally, this should be handled in a task queue, but we want to stay within
    Heroku's free plan, so it will be handled inline instead.
    (A worker dyno costs money.)

    .. _JIRA's webhook docs: https://developer.atlassian.com/display/JIRADEV/JIRA+Webhooks+Overview
    """
    try:
        event = request.get_json()
    except ValueError:
        raise ValueError("Invalid JSON from JIRA: {data}".format(
            data=request.data.decode('utf-8')
        ))
    bugsnag_context = {"event": event}
    bugsnag.configure_request(meta_data=bugsnag_context)

    if app.debug:
        print(json.dumps(event), file=sys.stderr)

    if "issue" not in event:
        # It's rare, but we occasionally see junk data from JIRA. For example,
        # here's a real API request we've received on this handler:
        #   {"baseUrl": "https://openedx.atlassian.net",
        #    "key": "jira:1fec1026-b232-438f-adab-13b301059297",
        #    "newVersion": 64005, "oldVersion": 64003}
        # If we don't have an "issue" key, it's junk.
        return "What is this shit!?", 400

    return issue_opened(event["issue"], bugsnag_context)


def should_transition(issue):
    """
    Return a boolean indicating if the given issue should be transitioned
    automatically from "Needs Triage" to an open status.
    """
    issue_key = to_unicode(issue["key"])
    issue_status = to_unicode(issue["fields"]["status"]["name"])
    project_key = to_unicode(issue["fields"]["project"]["key"])
    if issue_status != "Needs Triage":
        print(
            "{key} has status {status}, does not need to be processed".format(
                key=issue_key, status=issue_status,
            ),
            file=sys.stderr,
        )
        return False

    # Open source pull requests do not skip Needs Triage.
    # However, if someone creates a subtask on an OSPR issue, that subtasks
    # might skip Needs Triage (it just follows the rest of the logic in this
    # function.)
    is_subtask = issue["fields"]["issuetype"]["subtask"]
    if project_key == "OSPR" and not is_subtask:
        print(
            "{key} is an open source pull request, and does not need to be processed.".format(
                key=issue_key
            ),
            file=sys.stderr,
        )
        return False

    user_url = URLObject(issue["fields"]["creator"]["self"])
    user_url = user_url.set_query_param("expand", "groups")

    user_resp = jira_get(user_url)
    if not user_resp.ok:
        raise requests.exceptions.RequestException(user_resp.text)

    user = user_resp.json()
    user_group_map = {g["name"]: g["self"] for g in user["groups"]["items"]}
    user_groups = set(user_group_map)

    exempt_groups = {
        # group name: set of projects that they can create non-triage issues
        "edx-employees": set(("ALL",)),
        "clarice": set(("MOB",)),
        "bnotions": set(("MOB",)),
    }
    for user_group in user_groups:
        if user_group not in exempt_groups:
            continue
        exempt_projects = exempt_groups[user_group]
        if "ALL" in exempt_projects:
            return True
        if project_key in exempt_projects:
            return True

    return False


def issue_opened(issue, bugsnag_context=None):
    bugsnag_context = bugsnag_context or {}
    bugsnag_context = {"issue": issue}
    bugsnag.configure_request(meta_data=bugsnag_context)

    issue_key = to_unicode(issue["key"])
    issue_url = URLObject(issue["self"])

    transitioned = False
    if should_transition(issue):
        transitions_url = issue_url.with_path(issue_url.path + "/transitions")
        transitions_resp = jira_get(transitions_url)
        if not transitions_resp.ok:
            raise requests.exceptions.RequestException(transitions_resp.text)
        transitions = {t["name"]: t["id"] for t in transitions_resp.json()["transitions"]}
        if "Open" in transitions:
            new_status = "Open"
        elif "Design Backlog" in transitions:
            new_status = "Design Backlog"
        else:
            raise ValueError("No valid transition! Possibilities are {}".format(transitions.keys()))

        body = {
            "transition": {
                "id": transitions[new_status],
            }
        }
        transition_resp = jira.post(transitions_url, json=body)
        if not transition_resp.ok:
            raise requests.exceptions.RequestException(transition_resp.text)
        transitioned = True

    # log to stderr
    action = "Transitioned to Open" if transitioned else "ignored"
    print(
        "{key} created by {name} ({username}), {action}".format(
            key=issue_key,
            name=to_unicode(issue["fields"]["creator"]["displayName"]),
            username=to_unicode(issue["fields"]["creator"]["name"]),
            action="Transitioned to Open" if transitioned else "ignored",
        ),
        file=sys.stderr,
    )
    return action


def github_pr_repo(issue):
    custom_fields = get_jira_custom_fields()
    pr_repo = issue["fields"].get(custom_fields["Repo"])
    parent_ref = parent_ref = issue["fields"].get("parent")
    if not pr_repo and parent_ref:
        parent = get_jira_issue(parent_ref["key"])
        pr_repo = parent["fields"].get(custom_fields["Repo"])
    return pr_repo


def github_pr_num(issue):
    custom_fields = get_jira_custom_fields()
    pr_num = issue["fields"].get(custom_fields["PR Number"])
    parent_ref = parent_ref = issue["fields"].get("parent")
    if not pr_num and parent_ref:
        parent = get_jira_issue(parent_ref["key"])
        pr_num = parent["fields"].get(custom_fields["PR Number"])
    try:
        return int(pr_num)
    except:
        return None


def github_pr_url(issue):
    """
    Return the pull request URL for the given JIRA issue,
    or raise an exception if they can't be determined.
    """
    pr_repo = github_pr_repo(issue)
    pr_num = github_pr_num(issue)
    if not pr_repo or not pr_num:
        issue_key = to_unicode(issue["key"])
        fail_msg = '{key} is missing "Repo" or "PR Number" fields'.format(key=issue_key)
        raise Exception(fail_msg)
    return "/repos/{repo}/pulls/{num}".format(repo=pr_repo, num=pr_num)


@app.route("/jira/issue/updated", methods=("POST",))
def jira_issue_updated():
    """
    Received an "issue updated" event from JIRA. See `JIRA's webhook docs`_.

    .. _JIRA's webhook docs: https://developer.atlassian.com/display/JIRADEV/JIRA+Webhooks+Overview
    """
    try:
        event = request.get_json()
    except ValueError:
        raise ValueError("Invalid JSON from JIRA: {data}".format(
            data=request.data.decode('utf-8')
        ))
    bugsnag_context = {"event": event}
    bugsnag.configure_request(meta_data=bugsnag_context)

    if app.debug:
        print(json.dumps(event), file=sys.stderr)

    if "issue" not in event:
        # It's rare, but we occasionally see junk data from JIRA. For example,
        # here's a real API request we've received on this handler:
        #   {"baseUrl": "https://openedx.atlassian.net",
        #    "key": "jira:1fec1026-b232-438f-adab-13b301059297",
        #    "newVersion": 64005, "oldVersion": 64003}
        # If we don't have an "issue" key, it's junk.
        return "What is this shit!?", 400

    # is this a comment?
    comment = event.get("comment")
    if comment:
        return jira_issue_comment_added(event["issue"], comment, bugsnag_context)

    # is the issue an open source pull request?
    if event["issue"]["fields"]["project"]["key"] != "OSPR":
        return "I don't care"

    # we don't care about OSPR subtasks
    if event["issue"]["fields"]["issuetype"]["subtask"]:
        return "ignoring subtasks"

    # is there a changelog?
    changelog = event.get("changelog")
    if not changelog:
        # it was just someone adding a comment
        return "I don't care"

    # did the issue change status?
    status_changelog_items = [item for item in changelog["items"] if item["field"] == "status"]
    if len(status_changelog_items) == 0:
        return "I don't care"

    pr_repo = github_pr_repo(event["issue"])
    if not pr_repo:
        issue_key = to_unicode(event["issue"]["key"])
        fail_msg = '{key} is missing "Repo" field'.format(key=issue_key)
        raise Exception(fail_msg)
    repo_labels_resp = github.get("/repos/{repo}/labels".format(repo=pr_repo))
    if not repo_labels_resp.ok:
        raise requests.exceptions.RequestException(repo_labels_resp.text)
    # map of label name to label URL
    repo_labels = {l["name"]: l["url"] for l in repo_labels_resp.json()}
    # map of label name lowercased to label name in the case that it is on Github
    repo_labels_lower = {name.lower(): name for name in repo_labels}

    old_status = status_changelog_items[0]["fromString"]
    new_status = status_changelog_items[0]["toString"]

    changes = []
    if new_status == "Rejected":
        change = jira_issue_rejected(event["issue"], bugsnag_context)
        changes.append(change)

    if new_status.lower() in repo_labels_lower:
        change = jira_issue_status_changed(event["issue"], event["changelog"], bugsnag_context)
        changes.append(change)

    if changes:
        return "\n".join(changes)
    else:
        return "no change necessary"


def jira_issue_rejected(issue, bugsnag_context=None):
    bugsnag_context = bugsnag_context or {}
    issue_key = to_unicode(issue["key"])

    pr_num = github_pr_num(issue)
    pr_url = github_pr_url(issue)
    issue_url = pr_url.replace("pulls", "issues")

    gh_issue_resp = github.get(issue_url)
    if not gh_issue_resp.ok:
        raise requests.exceptions.RequestException(gh_issue_resp.text)
    gh_issue = gh_issue_resp.json()
    bugsnag_context["github_issue"] = gh_issue
    bugsnag.configure_request(meta_data=bugsnag_context)
    if gh_issue["state"] == "closed":
        # nothing to do
        msg = "{key} was rejected, but PR #{num} was already closed".format(
            key=issue_key, num=pr_num
        )
        print(msg, file=sys.stderr)
        return msg

    # Comment on the PR to explain to look at JIRA
    username = to_unicode(gh_issue["user"]["login"])
    comment = {"body": (
        "Hello @{username}: We are unable to continue with "
        "review of your submission at this time. Please see the "
        "associated JIRA ticket for more explanation.".format(username=username)
    )}
    comment_resp = github.post(issue_url + "/comments", json=comment)

    # close the pull request on Github
    close_resp = github.patch(pr_url, json={"state": "closed"})
    if not close_resp.ok or not comment_resp.ok:
        bugsnag_context['request_headers'] = close_resp.request.headers
        bugsnag_context['request_url'] = close_resp.request.url
        bugsnag_context['request_method'] = close_resp.request.method
        bugsnag.configure_request(meta_data=bugsnag_context)
        bug_text = ''
        if not close_resp.ok:
            bug_text += "Failed to close; " + close_resp.text
        if not comment_resp.ok:
            bug_text += "Failed to comment on the PR; " + comment_resp.text
        raise requests.exceptions.RequestException(bug_text)
    return "Closed PR #{num}".format(num=pr_num)


def jira_issue_status_changed(issue, changelog, bugsnag_context=None):
    bugsnag_context = bugsnag_context or {}
    pr_num = github_pr_num(issue)
    pr_repo = github_pr_repo(issue)
    pr_url = github_pr_url(issue)
    issue_url = pr_url.replace("pulls", "issues")

    status_changelog = [item for item in changelog["items"] if item["field"] == "status"][0]
    old_status = status_changelog["fromString"]
    new_status = status_changelog["toString"]

    # get github issue
    gh_issue_resp = github.get(issue_url)
    if not gh_issue_resp.ok:
        raise requests.exceptions.RequestException(gh_issue_resp.text)
    gh_issue = gh_issue_resp.json()

    # get repo labels
    repo_labels_resp = github.get("/repos/{repo}/labels".format(repo=pr_repo))
    if not repo_labels_resp.ok:
        raise requests.exceptions.RequestException(repo_labels_resp.text)
    # map of label name to label URL
    repo_labels = {l["name"]: l["url"] for l in repo_labels_resp.json()}
    # map of label name lowercased to label name in the case that it is on Github
    repo_labels_lower = {name.lower(): name for name in repo_labels}

    # Get all the existing labels on this PR
    pr_labels = [label["name"] for label in gh_issue["labels"]]
    print("old labels: {}".format(pr_labels), file=sys.stderr)

    # remove old status label
    old_status_label = repo_labels_lower.get(old_status.lower(), old_status)
    print("old status label: {}".format(old_status_label), file=sys.stderr)
    if old_status_label in pr_labels:
        pr_labels.remove(old_status_label)
    # add new status label
    new_status_label = repo_labels_lower[new_status.lower()]
    print("new status label: {}".format(new_status_label), file=sys.stderr)
    if new_status_label not in pr_labels:
        pr_labels.append(new_status_label)

    print("new labels: {}".format(pr_labels), file=sys.stderr)

    # Update labels on github
    update_label_resp = github.patch(issue_url, json={"labels": pr_labels})
    if not update_label_resp.ok:
        raise requests.exceptions.RequestException(update_label_resp.text)
    return "Changed labels of PR #{num} to {labels}".format(num=pr_num, labels=pr_labels)


def jira_issue_comment_added(issue, comment, bugsnag_context=None):
    bugsnag_context = bugsnag_context or {}
    issue_key = to_unicode(issue["key"])

    # we want to parse comments on Course Launch issues to fill out the cert report
    # see https://openedx.atlassian.net/browse/TOOLS-19
    if issue["fields"]["project"]["key"] != "COR":
        return "I don't care"

    lines = comment['body'].splitlines()
    if len(lines) < 2:
        return "I don't care"

    # the comment that we want should have precisely these headings in this order
    headings = [
        "course ID", "audit", "audit_enrolled", "downloadable",
        "enrolled_current", "enrolled_total", "honor", "honor_enrolled",
        "notpassing", "verified", "verified_enrolled",
    ]
    HEADING_RE = re.compile(r"\w+".join(headings))
    TIMESTAMP_RE = re.compile(r"^\d\d:\d\d:\d\d ")

    # test header/content pairs
    values = None
    for header, content in zip(lines, lines[1:]):
        # if both header and content start with a timestamp, chop it off
        if TIMESTAMP_RE.match(header) and TIMESTAMP_RE.match(content):
            header = header[9:]
            content = content[9:]

        # does this have the headings we're expecting?
        if not HEADING_RE.search(header):
            # this is not the header, move on
            continue

        # this must be it! grab the values
        values = content.split()

        # check that we have the right number
        if len(values) == len(headings):
            # we got it!
            break
        else:
            # aww, we were so close...
            values = None

    if not values:
        return "Didn't find header/content pair"

    custom_fields = get_jira_custom_fields()
    fields = {
        custom_fields["Course ID"]: values[0],
        custom_fields["?"]: int(values[1]), # "audit"
        custom_fields["Enrolled Audit"]: int(values[2]),
        custom_fields["?"]: int(values[3]), # "downloadable"
        custom_fields["Current Enrolled"]: int(values[4]),
        custom_fields["Total Enrolled"]: int(values[5]),
        custom_fields["?"]: int(values[6]), # "honor"
        custom_fields["Enrolled Honor Code"]: int(values[7]),
        custom_fields["Not Passing"]: int(values[8]),
        custom_fields["?"]: int(values[9]), # "verified"
        custom_fields["Enrolled Verified"]: int(values[10]),
    }
    issue_url = issue["self"]
    update_resp = jira.put(issue_url, json={"fields": fields})
    if not update_resp.ok:
        raise requests.exceptions.RequestException(update_resp.text)
    return "{key} cert info updated".format(key=issue_key)


@app.route("/jira/user/rescan", methods=("GET", "POST"))
def jira_rescan_users():
    """
    This task goes through all users on JIRA and ensures that they are assigned
    to the correct group based on the user's email address. It's meant to be
    run regularly: once an hour or so.
    """
    # a mapping of group name to email domain
    domain_groups = {
        "edx-employees": "@edx.org",
        "clarice": "@claricetechnologies.com",
        "bnotions": "@bnotions.com",
    }
    if request.method == "GET":
        return render_template("jira_rescan_users.html", domain_groups=domain_groups)

    failures = defaultdict(dict)

    requested_group = request.form.get("group")
    if requested_group:
        if requested_group not in domain_groups:
            resp = jsonify({"error": "Not found", "groups": domain_groups.keys()})
            resp.status_code = 404
            return resp
        requested_groups = {requested_group: domain_groups[requested_group]}
    else:
        requested_groups = domain_groups

    for groupname, domain in requested_groups.items():
        users_in_group = jira_group_members(groupname, session=jira, debug=True)
        usernames_in_group = set(u["name"] for u in users_in_group)
        bugsnag_context = {
            "groupname": groupname,
            "usernames_in_group": usernames_in_group,
        }
        bugsnag.configure_request(meta_data=bugsnag_context)

        for user in jira_users(filter=domain, session=jira, debug=True):
            if not user["email"].endswith(domain):
                pass
            username = user["name"]
            if username not in usernames_in_group:
                # add the user to the group!
                resp = jira.post(
                    "/rest/api/2/group/user?groupname={}".format(groupname),
                    json={"name": username},
                )
                if not resp.ok:
                    failures[groupname][username] = resp.text

    resp = jsonify(failures)
    resp.status_code = 502 if failures else 200
    return resp
