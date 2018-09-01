import sublime
import sublime_plugin

import hashlib
import htmlmin
import json
import os
from http.client import CannotSendRequest
from jinja2 import Template
from os.path import realpath
from threading import Lock


from ..lib import deferred, keymap, link_opener, logger, settings, requests


__all__ = [
    'EventDispatcher',
    'CompletionsHandler',
    'SignaturesHandler',
    'HoverHandler',
    'StatusHandler',
]


_DEBUG = os.getenv('SUBLIME_DEV')

def _is_view_supported(view):
    return view.file_name() is not None and view.file_name().endswith('.py')

def _check_view_size(view):
    return view.size() <= (1 << 20)

def _in_function_call(view, point):
    return (view.match_selector(point, 'meta.function-call.python') and
            not view.match_selector(point, 'variable.function.python'))

def md5(text):
    return hashlib.md5(str.encode(text)).hexdigest()


class EventDispatcher(sublime_plugin.EventListener):
    """Listener which forwards editor events to the event endpoint and also
    fetches completions and function signature information when the proper
    event triggers are fired.
    """

    _last_selection_region = None

    def on_modified(self, view):
        self._handle(view, 'edit')

    def on_selection_modified(self, view):
        self._handle(view, 'selection')

    @classmethod
    def _handle(cls, view, action):
        if not _is_view_supported(view):
            return

        deferred.defer(requests.kited_post, '/clientapi/editor/event',
                       data=cls._event_data(view, action))

        if action == 'selection':
            select_region = cls._view_region(view)
            cls._last_selection_region = select_region

            if _in_function_call(view, select_region['end']):
                if SignaturesHandler.is_activated():
                    SignaturesHandler.queue_signatures(view,
                                                       select_region['end'])
            else:
                SignaturesHandler.hide_signatures(view)

        if action == 'edit' and _check_view_size(view):
            edit_region = cls._view_region(view)
            edit_type, num_chars = cls._edit_info(cls._last_selection_region,
                                                  edit_region)

            if edit_type == 'insertion' and num_chars == 1:
                CompletionsHandler.queue_completions(view, edit_region['end'])
            elif edit_type == 'deletion' and num_chars > 1:
                CompletionsHandler.hide_completions(view)

            if _in_function_call(view, edit_region['end']):
                SignaturesHandler.queue_signatures(view, edit_region['end'])
            else:
                SignaturesHandler.hide_signatures(view)

    @staticmethod
    def _view_region(view):
        if len(view.sel()) != 1:
            return None

        r = view.sel()[0]
        return {
            'file': view.file_name(),
            'begin': r.begin(),
            'end': r.end(),
        }

    @staticmethod
    def _edit_info(selection, edit):
        no_info = (None, None)

        if (selection is None or edit is None or
            selection['file'] != edit['file']):
            return no_info

        if (edit['end'] > selection['end']):
            return ('insertion', edit['end'] - selection['end'])
        if (edit['end'] < selection['end']):
            return ('deletion', selection['end'] - edit['end'])

        return no_info

    @staticmethod
    def _event_data(view, action):
        text = view.substr(sublime.Region(0, view.size()))

        if not _check_view_size(view):
            action = 'skip'
            text = ''

        return {
            'source': 'sublime3',
            'filename': realpath(view.file_name()),
            'text': text,
            'action': action,
            'selections': [{'start': r.a, 'end': r.b} for r in view.sel()],
        }


class CompletionsHandler(sublime_plugin.EventListener):
    """Listener which handles completions by preemptively forwarding requests
    to the completions endpoint and then running the Sublime `auto_complete`
    command.
    """

    _received_completions = []
    _last_location = None
    _lock = Lock()

    def on_query_completions(self, view, prefix, locations):
        cls = self.__class__

        if not _is_view_supported(view):
            return None

        if not _check_view_size(view):
            return None

        if len(locations) != 1:
            return None

        with cls._lock:
            if (cls._last_location != locations[0] and
                cls._received_completions):
                logger.log('completions location mismatch: {} != {}'
                           .format(cls._last_location, locations[0]))

            completions = None
            if (cls._last_location == locations[0] and
                cls._received_completions):
                completions = [
                    (self._brand_completion(c['display'], c['hint']),
                     c['insert']) for c in cls._received_completions
                ]
            cls._received_completions = []
            cls._last_location = None
            return completions

    @classmethod
    def queue_completions(cls, view, location):
        deferred.defer(cls._request_completions,
                       view, cls._event_data(view, location))

    @classmethod
    def hide_completions(cls, view):
        with cls._lock:
            cls._received_completions = []
            cls._last_location = None
        view.run_command('hide_auto_complete')

    @classmethod
    def _request_completions(cls, view, data):
        resp, body = requests.kited_post('/clientapi/editor/completions', data)

        if resp.status != 200 or not body:
            return

        try:
            resp_data = json.loads(body.decode('utf-8'))
            completions = resp_data['completions'] or []
            with cls._lock:
                cls._received_completions = completions
                cls._last_location = data['cursor_runes']
            cls._run_auto_complete(view)
        except ValueError as ex:
            logger.log('error decoding json: {}'.format(ex))

    @staticmethod
    def _run_auto_complete(view):
        view.run_command('auto_complete', {
            'api_completions_only': True,
            'disable_auto_insert': True,
            'next_completion_if_showing': False,
        })

    @staticmethod
    def _brand_completion(symbol, hint=None):
        return ('{}\t{} ⟠'.format(symbol, hint) if hint
                else '{}\t⟠'.format(symbol))

    @staticmethod
    def _event_data(view, location):
        return {
            'filename': realpath(view.file_name()),
            'editor': 'sublime3',
            'text': view.substr(sublime.Region(0, view.size())),
            'cursor_runes': location,
        }


class SignaturesHandler(sublime_plugin.EventListener):
    """Listener which handles signatures by sending requests to the signatures
    endpoint and rendering the returned data.
    """

    _activated = False
    _view = None
    _call = None
    _lock = Lock()

    _template_path = 'Packages/KPP/lib/assets/function-signature-panel.html'
    _template = None
    _css_path = 'Packages/KPP/lib/assets/styles.css'
    _css = ''

    def on_post_text_command(self, view, command_name, args):
        if command_name in ('toggle_popular_patterns',
                            'toggle_keyword_arguments'):
            self.__class__._rerender()

    @classmethod
    def queue_signatures(cls, view, location):
        deferred.defer(cls._request_signatures,
                       view, cls._event_data(view, location))

    @classmethod
    def hide_signatures(cls, view):
        reset = False
        if cls._lock.acquire(blocking=False):
            cls._activated = False
            cls._view = None
            cls._call = None
            reset = True
            cls._lock.release()

        if reset:
            view.hide_popup()

    @classmethod
    def is_activated(cls):
        return cls._activated

    @classmethod
    def _request_signatures(cls, view, data):
        resp, body = requests.kited_post('/clientapi/editor/signatures', data)

        if resp.status != 200 or not body:
            if resp.status in (400, 404):
                cls.hide_signatures(view)
            return

        try:
            resp_data = json.loads(body.decode('utf-8'))
            calls = resp_data['calls'] or []
            if len(calls):
                call = calls[0]

                if call['callee']['kind'] == 'type':
                    call['callee']['details']['function'] = (
                        call['callee']['details']['type']['language_details']
                            ['python']['constructor'])

                # Separate out the keyword-only parameters
                func = call['callee']['details']['function']
                func.update({
                    'positional_parameters': [],
                    'keyword_only_parameters': [],
                })
                for _, param in enumerate(func['parameters'] or []):
                    param_details = param['language_details']['python']
                    if not param_details['keyword_only']:
                        func['positional_parameters'].append(param)
                    else:
                        func['keyword_only_parameters'].append(param)

                in_kwargs = call['language_details']['python']['in_kwargs']
                logger.log('call: {} index = {}'
                           .format('kwarg' if in_kwargs else 'arg',
                                   call['arg_index']))

                content = None
                if cls._lock.acquire(blocking=False):
                    cls._activated = True
                    cls._view = view
                    cls._call = call
                    content = cls._render(call)
                    cls._lock.release()

                if content is not None:
                    view.show_popup(content,
                                    flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
                                    max_width=400,
                                    on_navigate=cls._handle_link_click)

        except ValueError as ex:
            logger.log('error decoding json: {}'.format(ex))

    @classmethod
    def _render(cls, call):
        if _DEBUG or cls._template is None:
            cls._template = Template(sublime.load_resource(cls._template_path))
            cls._css = sublime.load_resource(cls._css_path)

        opts = {
            'show_popular_patterns': settings.get('show_popular_patterns'),
            'show_keyword_arguments': settings.get('show_keyword_arguments'),
            'keyword_argument_highlighted': cls._kwarg_highlighted(),
            'keyword_arguments_keys': keymap.get('toggle_keyword_arguments'),
            'popular_patterns_keys': keymap.get('toggle_popular_patterns'),
        }

        return htmlmin.minify(cls._template.render(css=cls._css, call=call,
                                                   **opts),
                              remove_all_empty_space=True)

    @classmethod
    def _rerender(cls):
        content = None
        if cls._lock.acquire(blocking=False):
            content = cls._render(cls._call) if cls._activated else None
            cls._lock.release()

        if content is not None:
            cls._view.show_popup(content,
                                 flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
                                 max_width=400,
                                 on_navigate=cls._handle_link_click)

    @classmethod
    def _handle_link_click(cls, target):
        if target == 'hide_popular_patterns':
            settings.set('show_popular_patterns', False)
            cls._rerender()

        elif target == 'show_popular_patterns':
            settings.set('show_popular_patterns', True)
            cls._rerender()

        elif target == 'hide_keyword_arguments':
            settings.set('show_keyword_arguments', False)
            cls._rerender()

        elif target == 'show_keyword_arguments':
            settings.set('show_keyword_arguments', True)
            cls._rerender()

        elif (target.startswith('open_browser') or
              target.startswith('open_copilot')):
            idx = target.find(':')
            if idx == -1:
                logger.log('invalid open link format: {}'.format(target))
                return

            action = target[:idx]
            ident = target[idx+1:]

            if action == 'open_browser':
                link_opener.open_browser(ident)
            else:
                link_opener.open_copilot(ident)

    @classmethod
    def _kwarg_highlighted(cls):
        return (cls._activated and
                cls._call['language_details']['python']['in_kwargs'] and
                cls._call['arg_index'] != -1)

    @staticmethod
    def _event_data(view, location):
        return {
            'editor': 'sublime3',
            'filename': realpath(view.file_name()),
            'text': view.substr(sublime.Region(0, view.size())),
            'cursor_runes': location,
        }


class HoverHandler(sublime_plugin.EventListener):
    """Listener which listens to the user's mouse position and forwards
    requests to the hover endpoint.
    """

    _template_path = 'Packages/KPP/lib/assets/hover-panel.html'
    _template = None
    _css_path = 'Packages/KPP/lib/assets/styles.css'
    _css = ''

    def on_hover(self, view, point, hover_zone):
        if (_is_view_supported(view) and _check_view_size(view) and
            len(view.sel()) == 1):
            cls = self.__class__
            deferred.defer(cls._request_hover, view, point)

    @classmethod
    def _request_hover(cls, view, point):
        resp, body = requests.kited_get(cls._event_url(view, point))

        if resp.status != 200 or not body:
            return

        try:
            resp_data = json.loads(body.decode('utf-8'))

            if resp_data['symbol'] is None:
                return

            symbol = resp_data['symbol'][0]
            if symbol['value'][0]['kind'] != 'instance':
                symbol['hint'] = symbol['value'][0]['kind']
            else:
                symbol['hint'] = symbol['value'][0]['type']

            view.show_popup(cls._render(resp_data['symbol'][0],
                                        resp_data['report']),
                            flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                            max_width=1024,
                            location=point,
                            on_navigate=cls._handle_link_click)
        except ValueError as ex:
            logger.log('error decoding json: {}'.format(ex))

    @classmethod
    def _render(cls, symbol, report):
        if _DEBUG or cls._template is None:
            cls._template = Template(sublime.load_resource(cls._template_path))
            cls._css = sublime.load_resource(cls._css_path)

        return htmlmin.minify(cls._template.render(css=cls._css, symbol=symbol,
                                                   report=report),
                              remove_all_empty_space=True)

    @classmethod
    def _handle_link_click(cls, target):
        if (target.startswith('open_browser') or
            target.startswith('open_copilot')):
            idx = target.find(':')
            if idx == -1:
                logger.log('invalid open link format: {}'.format(target))
                return

            action = target[:idx]
            ident = target[idx+1:]

            if action == 'open_browser':
                link_opener.open_browser(ident)
            else:
                link_opener.open_copilot(ident)

        elif target.startswith('open_definition'):
            idx = target.find(':')
            if idx == -1:
                logger.log('invalid open definition format: {}'.format(target))
                return

            dest = target[idx+1:]
            if not dest[dest.rfind(':')+1:].isdigit():
                logger.log('invalid open definition format: {}'.format(target))
                return

            sublime.active_window().open_file(dest,
                                              flags=sublime.ENCODED_POSITION)

    @staticmethod
    def _event_url(view, point):
        editor = 'sublime3'
        filename = realpath(view.file_name()).replace('/', ':')
        hash_ = md5(view.substr(sublime.Region(0, view.size())))
        return ('/api/buffer/{}/{}/{}/hover?cursor_runes={}'
                .format(editor, filename, hash_, point))


class StatusHandler(sublime_plugin.EventListener):
    """Listener which sets the status bar message when the view is activated
    and on every selection event.
    """

    _status_key = 'kite'

    def on_activated(self, view):
        deferred.defer(self.__class__._handle, view)

    def on_selection_modified(self, view):
        deferred.defer(self.__class__._handle, view)

    @classmethod
    def _handle(cls, view):
        if not _is_view_supported(view):
            view.erase_status(cls._status_key)
            return

        if not _check_view_size(view):
            view.set_status(cls._status_key,
                            cls._brand_status('File too large'))
            return

        try:
            url = ('/clientapi/status?filename={}'
                   .format(realpath(view.file_name())))
            resp, body = requests.kited_get(url)

            if resp.status != 200 or not body:
                view.set_status(cls._status_key,
                                cls._brand_status('Server error'))
            else:
                resp_data = json.loads(body.decode('utf-8'))
                status = cls._brand_status(resp_data['status'].capitalize())
                view.set_status(cls._status_key, status)

        except ConnectionRefusedError as ex:
            view.set_status(cls._status_key,
                            cls._brand_status('Connection error'))

        except CannotSendRequest as ex:
            logger.log('could not request status: {}'.format(ex))

        except ValueError as ex:
            logger.log('error decoding json: {}'.format(ex))

    @classmethod
    def _brand_status(cls, status):
        return '𝕜𝕚𝕥𝕖: {}'.format(status)
