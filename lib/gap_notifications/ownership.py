"""Live ownership policy with anonymous GitLab and explicit GitHub access."""
import re
import yaml
import requests
from lib.gap_component_map import normalize_component_name

BASE = "https://gitlab.cee.redhat.com/wznoinsk/rhoai-monitoring/-/raw/main/data/"
REPOSITORY_URL = 'https://api.github.com/repos/red-hat-data-services/rhoai-ops/contents/config/automerge_team_mapping.yaml?ref=main'
REPOSITORY_LINK = 'https://github.com/red-hat-data-services/rhoai-ops/blob/main/config/automerge_team_mapping.yaml'
LIMIT = 1024 * 1024
MAPPING_LINK_BASE = "https://gitlab.cee.redhat.com/wznoinsk/rhoai-monitoring/-/blob/main/data/"


def fetch_mapping(name, *, ca_bundle=None, github_token=None):
    repository = name == 'automerge_team_mapping.yaml'
    options = {}
    if repository:
        if not isinstance(github_token, str) or not github_token.strip():
            raise ValueError("GitHub token required for repository ownership mapping")
        # Explicit credentials go only to this fixed API URL, never GitLab or
        # redirects. Do not reuse sessions between the two trust domains.
        options['headers'] = {'Authorization': f'Bearer {github_token}',
                              'Accept': 'application/vnd.github.raw+json'}
    session = requests.Session()
    session.trust_env = False
    try:
        with session.get(REPOSITORY_URL if repository else BASE + name,
                         timeout=(5, 15), allow_redirects=False,
                         stream=True, verify=True if repository else (ca_bundle or True),
                         **options) as response:
            if response.status_code != 200:
                raise ValueError("repository ownership mapping unavailable" if repository
                                 else "ownership mapping unavailable")
            body = bytearray()
            for chunk in response.iter_content(8192):
                body.extend(chunk)
                if len(body) > LIMIT:
                    raise ValueError("ownership mapping too large")
            return bytes(body)
    finally:
        session.close()


def _names(value):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or any(
            not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
        raise ValueError("invalid Jira ownership")
    return list(dict.fromkeys(values))


def _repository(value):
    if not isinstance(value, str):
        return None
    value = value.lower()
    for prefix in ('https://github.com/', 'github.com/'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if re.fullmatch(r'[a-z0-9_.-]+', value):
        value = 'red-hat-data-services/' + value
    if not re.fullmatch(r'red-hat-data-services/[a-z0-9_.-]+', value):
        return None
    return value


class OwnershipPolicy:
    def __init__(self, jira, teams, repositories=None):
        if not isinstance(jira, dict) or not isinstance(teams, dict):
            raise ValueError("invalid ownership mapping")
        mappings = jira.get('mappings')
        if not isinstance(mappings, dict):
            raise ValueError("missing Jira mappings")
        if any(not isinstance(k, str) or not k.strip() for k in mappings):
            raise ValueError("invalid Jira ownership key")
        self.jira = {k: _names(v) for k, v in mappings.items()}
        self.default_jira = _names(jira.get('_default', 'DevOps'))
        components = teams.get('components')
        if not isinstance(components, dict) or not isinstance(components.get('default'), dict):
            raise ValueError("missing Slack default")
        default_handle = components['default'].get('slack_team_handle')
        if (not isinstance(default_handle, str) or default_handle == 'unknown'
                or default_handle.lower() in {'here', 'channel', 'everyone'}
                or not re.fullmatch(r'[a-zA-Z0-9_-]+', default_handle)):
            raise ValueError("invalid Slack default handle")
        self.teams = {}
        for name, value in components.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
                raise ValueError("invalid Slack ownership")
            handle = value.get('slack_team_handle', default_handle)
            if handle == 'unknown':
                handle = default_handle
            if (not isinstance(handle, str) or handle.lower() in {'here', 'channel', 'everyone'}
                    or not re.fullmatch(r'[a-zA-Z0-9_-]+', handle)):
                raise ValueError("invalid Slack handle")
            self.teams[name] = handle

        self.repositories = {}
        self.repository_teams = {}
        if repositories is not None:
            if not isinstance(repositories, dict):
                raise ValueError('invalid repository ownership')
            for field, target, slack in (
                    ('repo_to_component', self.repositories, False),
                    ('repo_to_slack_handle', self.repository_teams, True)):
                mapping = repositories.get(field, {} if slack else None)
                if not isinstance(mapping, dict):
                    raise ValueError('missing repository mapping')
                for key, value in mapping.items():
                    normalized = _repository(key)
                    if normalized is None or normalized in target:
                        raise ValueError('invalid or duplicate repository')
                    if slack:
                        if (not isinstance(value, str) or value.lower() in
                                {'here', 'channel', 'everyone', 'unknown'} or
                                not re.fullmatch(r'[a-zA-Z0-9_-]+', value)):
                            raise ValueError('invalid repository Slack handle')
                        target[normalized] = value
                    else:
                        target[normalized] = _names(value)

    @classmethod
    def load(cls, fetch=None, *, ca_bundle=None, github_token=None):
        fetch = fetch or (lambda name: fetch_mapping(name, ca_bundle=ca_bundle, github_token=github_token))
        jira = yaml.safe_load(fetch('konflux-to-jira-component-map.yaml'))
        teams = yaml.safe_load(fetch('rhoai-component-data.yaml'))
        repositories = yaml.safe_load(fetch('automerge_team_mapping.yaml'))
        if not isinstance(repositories, dict):
            raise ValueError('invalid fetched repository ownership')
        return cls(jira, teams, repositories)

    @staticmethod
    def _lookup(mapping, component, default):
        if not isinstance(component, str):
            return default
        return mapping.get(component, mapping.get(normalize_component_name(component), default))

    def extra_fields(self, observation):
        names = (self.repositories.get(_repository(observation.get('repository')), self.default_jira)
                 if observation.get('category') in {'conflict', 'feasibility'} else
                 self._lookup(self.jira, observation.get('component'), self.default_jira))
        return {'components': [{'name': name} for name in names]}

    def blocks(self, observation, text, *, root=False):
        """Integrate ownership with the summary; only the mapped handle is parsed."""
        blocks = []
        root_failure = root and observation.get("status") == "failure"
        if root_failure:
            handle = (self.repository_teams.get(_repository(observation.get('repository')), self.teams['default'])
                      if observation.get('category') in {'conflict', 'feasibility'} else
                      self._lookup(self.teams, observation.get("component"), self.teams["default"]))
            summary, _, body = text.partition("\n")
            if len(summary) > 2999:
                summary, body = "*Failure*", text
            elif body.startswith("*Component:*"):
                component, _, remaining = body.partition("\n")
                if len(summary) + len(component) + 2 <= 3000:
                    summary, body = summary + "\n" + component, remaining
            # Full-width sections avoid long names squeezing the ownership label.
            # Explicit newlines also separate sections when their text is copied.
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": (summary or "*Failure*") + "\n", "verbatim": True}})
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": "*Owning team:* @" + handle + "\n", "verbatim": False}})
            text = body
        blocks.extend({"type": "section", "text": {
            "type": "mrkdwn", "text": text[i:i + 3000], "verbatim": True}}
            for i in range(0, len(text), 3000))
        if root_failure:
            blocks.append({"type": "context", "elements": [{
                "type": "mrkdwn", "verbatim": True,
                "text": "Ownership mappings: "
                        f"<{MAPPING_LINK_BASE}rhoai-component-data.yaml|Slack teams> · "
                        f"<{MAPPING_LINK_BASE}konflux-to-jira-component-map.yaml|Jira components>"
                        + (f" · <{REPOSITORY_LINK}|Repository ownership>" if
                           observation.get('category') in {'conflict', 'feasibility'} else ''),
            }]})
        return blocks
