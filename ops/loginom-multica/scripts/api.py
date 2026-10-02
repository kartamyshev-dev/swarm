"""Use native Multica credentials; never log bearer material or response bodies."""
import json
import os
import urllib.request
import urllib.error
import urllib.parse


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        redirected = super().redirect_request(request, response, code, message, headers, newurl)
        if urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            for key in ['Authorization', 'X-workspace-id']:
                redirected.remove_header(key)
        return redirected


class API:
    def __init__(self):
        self.base = os.environ['MULTICA_SERVER_URL'].rstrip('/')
        self.headers = {'Authorization': 'Bearer ' + os.environ['MULTICA_TOKEN'],
                        'X-Workspace-ID': os.environ['MULTICA_WORKSPACE_ID'],
                        'Content-Type': 'application/json'}

    def request(self, path, data=None, method=None, binary=False):
        request = urllib.request.Request(self.base + '/api/' + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method or ('PUT' if data is not None else 'GET'), headers=self.headers)
        try:
            with urllib.request.build_opener(DownloadRedirect()).open(request, timeout=90) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError('MULTICA_HTTP_' + str(error.code)) from None
        except urllib.error.URLError:
            raise RuntimeError('MULTICA_UNAVAILABLE') from None
        return body if binary else json.loads(body)
