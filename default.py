import sublime
import sublime_plugin
import logging
import re
from threading import Timer

DEFAULT_LOG_LEVEL = logging.DEBUG
l = logging.getLogger(__name__)

PLUGIN_KEY = 'ScopedQuickSelect'

PREVIOUS_KEYWORDS_PER_VIEW = {}

class ScopedQuickSelect(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		scoped_quick_select(self, self.view, edit, args["scope"])

def scoped_quick_select(text_command, view, edit, target_scope):
	l.debug(str(view.id()) + ' scoped_quick_select(' + target_scope +')')
	all_sel = view.sel()

	first_sel = all_sel[0];

	cursor_scopes = view.scope_name(first_sel.a)
	num_blocks_of_cursor = cursor_scopes.count("meta.block")

	regex = ''
	if first_sel.size() < 1:
		l.debug('just a cursor: ')
		regex = '\\b' + view.substr(view.word(first_sel)) + '\\b'
	else:
		l.debug('some text selected: ')
		regex = view.substr(first_sel)

	matches = view.find_all(regex)

	scoped_matches = []
	if (target_scope == "all"):
		scoped_matches = matches

	elif (target_scope == "function"):
		l.warn('TODO: implement')

	elif (target_scope == "parens"):
		l.warn('TODO: implement')

	elif (target_scope == "block"):
		# TODO: Other language "blocks"
		search_end = first_sel.a;
		block_start = search_end;
		while True:
			# TODO: This is probably going to be a perf bottleneck
			text_before = view.substr(sublime.Region(0, search_end))
			block_start = text_before.rfind('{')

			if block_start < 0:
				block_start = 0
				l.debug('reached start of buffer')
				break

			search_end = block_start - 1
			brace_scopes = view.scope_name(block_start)
			if any(scope.split('.')[0] == "comment" for scope in brace_scopes.split(' ')):
				l.debug('commented open brace at ' + str(view.rowcol(block_start)))
				continue

			# TODO: Support languages that don't use meta.block
			# we basically have to match block delimiters ourselves...
			num_blocks_of_brace = brace_scopes.count("meta.block")
			if num_blocks_of_brace == num_blocks_of_cursor:
				l.debug('found start brace: ' + str(block_start))
				break

		search_start = first_sel.b;
		block_end = search_start;
		view_end = view.size()
		while True:
			# TODO: This is probably going to be a perf bottleneck
			text_after = view.substr(sublime.Region(search_start, view_end))
			block_end = text_after.find('}')

			if block_end < 0:
				block_end = view_end
				l.debug('reached end of buffer')
				break

			block_end += search_start
			search_start = block_end + 1

			brace_scopes = view.scope_name(block_end)
			if any(scope.split('.')[0] == "comment" for scope in brace_scopes.split(' ')):
				l.debug('commented close brace at ' + str(view.rowcol(block_start)))
				continue

			num_blocks_of_brace = brace_scopes.count("meta.block")
			if num_blocks_of_brace == num_blocks_of_cursor:
				l.debug('found end brace: ' + str(block_end))
				break

		block_region = sublime.Region(block_start, block_end)
		scoped_matches = [block_region.intersection(m) for m in matches]
	else:
		l.warn('Unimplemented match target_scope: ' + str(target_scope))

	scoped_matches = [m for m in scoped_matches if not m.empty()]

	if any(scoped_matches):
		last_match = scoped_matches[-1]
		view.show(last_match)
		all_sel.add_all(scoped_matches)

def on_activated_async(view):
	l.debug(str(view.id()) + ' on_activated_async')

def on_load_async(view):
	l.debug(str(view.id()) + ' on_load_async')

def settings_changed(view):
	l.debug(str(view.id()) + ' settings_changed')

class CustomScript(sublime_plugin.EventListener):
	registered_views = set()

	def __init__(self):
		pass

	def on_activated_async(self, view):
		if view.id() not in self.registered_views:
			l.debug('registering ' + str(view.id()))
			settings = view.settings()
			settings.clear_on_change(PLUGIN_KEY)
			settings.add_on_change(PLUGIN_KEY, lambda: settings_changed(view))
			self.registered_views.add(view.id())

		on_activated_async(view)

	def on_pre_close(self, view):
		l.debug('removing view ' + str(view.id()))
		self.registered_views.remove(view.id())

	def on_load_async(self, view):
		on_load_async(view)

def plugin_loaded():
	pl = logging.getLogger(__package__)
	for handler in pl.handlers[:]:
		pl.removeHandler(handler)

	handler = logging.StreamHandler()
	formatter = logging.Formatter(fmt="{asctime} [{name}] {levelname}: {message}",
								  style='{')
	handler.setFormatter(formatter)
	pl.addHandler(handler)

	pl.setLevel(DEFAULT_LOG_LEVEL)
	l.debug('plugin_loaded')
