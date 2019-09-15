import sublime
import sublime_plugin
import logging
import re
import os
import shutil
from threading import Timer

DEFAULT_LOG_LEVEL = logging.DEBUG
l = logging.getLogger(__name__)

PLUGIN_KEY = 'ScopedQuickSelect'
SCOPE_MARKERS_KEY = PLUGIN_KEY + 'scope_markers'

SET_SCOPE_PER_VIEW = {}
ALL_MATCHES_IN_SCOPE = {}

class ScopedQuickSelect(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		scoped_quick_select(self, self.view, edit, args["scope"])

class SetQuickSelectScope(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		set_quick_select_scope(self, self.view, edit, args["scope"])

class ClearQuickSelectScope(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		clear_quick_select_scope(self, self.view, edit)

class IncrementalQuickSelect(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		incremental_quick_select(self, self.view, edit, bool(args["add"]))

# TODO: use `view.match_selector()` instead?
def has_comment_scope(scopes):
	return any(scope.split('.')[0] == "comment" for scope in scopes.split(' '))

def get_quick_select_scope(view, first_sel, target_scope):
	scope_region = sublime.Region(0, 0)
	if (target_scope == "all"):
		scope_region = sublime.Region(0, view.size())
	elif (target_scope == "function"):
		l.warn('TODO: implement')
	elif (target_scope == "parentheses"):
		scope_region = get_delimited_scope_region(view, first_sel, '(', ')', 'parenthesis')
	elif (target_scope == "square brackets"):
		scope_region = get_delimited_scope_region(view, first_sel, '[', ']', 'square bracket')
	elif (target_scope == "angle brackets"):
		scope_region = get_delimited_scope_region(view, first_sel, '<', '>', 'angle bracket')
	elif (target_scope == "single quotes"):
		# TODO: Need to be careful about escaped quotes here
		l.warn('TODO: implement')
	elif (target_scope == "double quotes"):
		# TODO: Need to be careful about escaped quotes here
		l.warn('TODO: implement')
	elif (target_scope == "block"):
		cursor_scopes = view.scope_name(first_sel.a)
		num_blocks_of_cursor = cursor_scopes.count("meta.block")

		# TODO: multiple calls in a row to expand out by a block?
		# TODO: iterative one at a time mode with option to skip?
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
			if has_comment_scope(brace_scopes):
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
			if has_comment_scope(brace_scopes):
				l.debug('commented close brace at ' + str(view.rowcol(block_start)))
				continue

			num_blocks_of_brace = brace_scopes.count("meta.block")
			if num_blocks_of_brace == num_blocks_of_cursor:
				l.debug('found end brace: ' + str(block_end))
				break

		scope_region = sublime.Region(block_start, block_end)
	else:
		l.warn('Unimplemented match target_scope: ' + str(target_scope))

	return scope_region

def clear_quick_select_scope(text_command, view, edit):
	l_debug('view {view_id} clear_quick_select_scope()',
	        view_id = view.id())

	key = view.id()
	view.erase_regions(SCOPE_MARKERS_KEY)
	if key in SET_SCOPE_PER_VIEW:
		del SET_SCOPE_PER_VIEW[key]
		l_debug('Cleared scope for view ' + key)

def set_quick_select_scope(text_command, view, edit, target_scope):
	l_debug('view {view_id} set_quick_select_scope({target_scope})',
	        view_id = view.id(), target_scope = target_scope)
	all_sel = view.sel()
	first_sel = all_sel[0];

	scope_region = get_quick_select_scope(view, first_sel, target_scope)
	key = view.id()
	if (scope_region.empty()):
		if key in SET_SCOPE_PER_VIEW:
			del SET_SCOPE_PER_VIEW[key]
		l_debug('Cleared scope for view ' + key)
		view.erase_regions(SCOPE_MARKERS_KEY)
	else:
		l_debug('Set scope {start} to {end}',
				start=view.rowcol(scope_region.a),
				end=view.rowcol(scope_region.b))

		scope_markers = [sublime.Region(scope_region.a, scope_region.a),
						 sublime.Region(scope_region.b, scope_region.b)]

		view.add_regions(SCOPE_MARKERS_KEY, scope_markers,
		                 'scoped_quick_select.scope_marker',
		                 flags=sublime.DRAW_EMPTY)

		SET_SCOPE_PER_VIEW[key] = scope_region

def incremental_quick_select(text_command, view, edit, add):
	l_debug('view {view_id} incremental_quick_select({add})',
	        view_id = view.id(), add = add)
	pass

def get_delimited_scope_region(view, original_selection, open_delim, close_delim, name):
	search_end = original_selection.a
	intial_scopes = view.scope_name
	block_start = search_end
	num_unmatched_delimiters = 0
	open_delim_len = len(open_delim)
	close_delim_len = len(close_delim)
	while search_end > 0:
		# TODO: This is probably going to be a perf bottleneck
		text_before = view.substr(sublime.Region(0, search_end))
		block_start = text_before.rfind(open_delim)
		other_block_end = text_before.rfind(close_delim)

		if block_start < 0:
			l.debug('reached start of buffer')
			view.window().status_message('No matching open ' + name)
			return sublime.Region(0, 0)

		if other_block_end > block_start:
			other_delim_scopes = view.scope_name(other_block_end)
			if not has_comment_scope(other_delim_scopes):
				num_unmatched_delimiters += 1
			search_end = other_block_end - 1
			continue

		# One to the left of the _start_ of the delimiter
		search_end = block_start - 1
		delim_scopes = view.scope_name(block_start)
		# TODO: Allow scoping to delimiters inside comments? (e.g. like this)
		# I think I want this to be scoped to a single comment "block"
		# which means consecutive single line comments, or a single
		# block comment for languages that support them
		if has_comment_scope(delim_scopes):
			l.debug('commented open ' + name + ' at ' + str(view.rowcol(block_start)))
			continue

		if num_unmatched_delimiters == 0:
			break

		num_unmatched_delimiters -= 1


	search_start = original_selection.b;
	block_end = search_start;
	view_end = view.size()
	while search_start < view_end:
		# l_debug('Searching for close {name} between {start} to {end}',
		# 		name=name,
		# 		start=view.rowcol(search_start),
		# 		end=view.rowcol(view_end))

		# TODO: This is probably going to be a perf bottleneck
		text_after = view.substr(sublime.Region(search_start, view_end))
		block_end = text_after.find(close_delim)
		other_block_start = text_after.find(open_delim)

		if block_end < 0:
			l.debug('reached end of buffer')
			view.window().status_message('No matching close ' + name)
			return sublime.Region(0, 0)

		block_end += search_start

		if other_block_start > 0:
			other_block_start += search_start
			if other_block_start < block_end:
				other_delim_scopes = view.scope_name(other_block_start)
				if not has_comment_scope(other_delim_scopes):
					num_unmatched_delimiters += 1
					#l_debug('Found open {name} at {position}', name=name, position=view.rowcol(other_block_start))
				#else:
				#	l_debug('Found commented open {name} at {position}', name=name, position=view.rowcol(other_block_start))
				search_start = other_block_start + open_delim_len
				continue

		search_start = block_end + close_delim_len

		delim_scopes = view.scope_name(block_end)
		if has_comment_scope(delim_scopes):
			#l.debug('commented closed ' + name + ' at ' + str(view.rowcol(block_end)))
			continue

		if num_unmatched_delimiters == 0:
			break

		num_unmatched_delimiters -= 1

	block_start += open_delim_len
	l.debug(str(name) + ' scope bounds: ' + str(view.rowcol(block_start)) + ' to ' + str(view.rowcol(block_end)))
	scope_region = sublime.Region(block_start, block_end)
	return scope_region

def get_pattern_for_cursor():
	if first_sel.size() < 1:
		word_around_cursor = view.substr(view.word(first_sel))
		regex = '\\b' + re.escape(word_around_cursor) + '\\b'

		if l.isEnabledFor(logging.DEBUG):
			l_debug('just a cursor at {cursor_pos} inside the word `{word}`',
			        cursor_pos = view.rowcol(first_sel.a),
			        word = word_around_cursor)
	else:
		selected_text = view.substr(first_sel)
		regex = re.escape(selected_text)

		if l.isEnabledFor(logging.DEBUG):
			l_debug('some text `{selected_text}`'
			        ' selected at {start_pos} to {end_pos}',
			        selected_text = selected_text,
			        start_pos = view.rowcol(first_sel.a),
			        end_pos = view.rowcol(first_sel.b))

	return regex

def scoped_quick_select(text_command, view, edit, target_scope):
	l_debug('view {view_id} scoped_quick_select({target_scope})',
	        view_id = view.id(), target_scope = target_scope)
	all_sel = view.sel()
	first_sel = all_sel[0];

	regex = get_pattern_for_cursor()

	matches = view.find_all(regex)

	scope_region = get_quick_select_scope(view, first_sel, target_scope)
	if scope_region.empty():
		scoped_matches = []
	else:
		scoped_matches = [scope_region.intersection(m) for m in matches]

	scoped_matches = [m for m in scoped_matches if not m.empty()]

	if any(scoped_matches):
		last_match = scoped_matches[-1]
		view.show(last_match)
		all_sel.add_all(scoped_matches)

class ScopedQuickSelectListener(sublime_plugin.EventListener):
	registered_views = set()
	color_schemes = set()

	def __init__(self):
		pass

	def on_activated_async(self, view):
		if view.id() not in self.registered_views:
			self.on_first_activation_async(view)

		# Every activation:
		pass

	def on_first_activation_async(self, view):
		l.debug('registering ' + str(view.id()))

		settings = view.settings()
		settings.clear_on_change(PLUGIN_KEY)
		settings.add_on_change(PLUGIN_KEY, lambda: self.settings_changed(view))

		self.setup_color_scheme(view)

		self.registered_views.add(view.id())

	def on_pre_close(self, view):
		l.debug('removing view ' + str(view.id()))
		self.registered_views.remove(view.id())

	def on_load_async(self, view):
		on_load_async(view)

	def settings_changed(self, view):
		self.setup_color_scheme(view)

	def setup_color_scheme(self, view):
		current_color_scheme = view.settings().get("color_scheme")

		if current_color_scheme is None:
			return

		# NOTE: Only do it once per plugin activation.
		# We don't want to bail out if it already exists because we
		# want to be able to update the source and have it be copied
		# again then next time the plugin is loaded.
		if current_color_scheme in self.color_schemes:
			return

		self.color_schemes.add(current_color_scheme)

		plugin_dir = os.path.join(sublime.packages_path(), PLUGIN_KEY)

		# Copy our override rules to a new colour scheme file
		# inside our plugin directory, with the same name as the
		# active colour scheme.
		color_schemes_dir = os.path.join(plugin_dir, 'color_schemes')
		os.makedirs(color_schemes_dir, exist_ok = True)

		scheme_name = os.path.splitext(os.path.basename(current_color_scheme))[0]
		scheme_dest_path = os.path.join(color_schemes_dir, scheme_name + os.extsep + "sublime-color-scheme")

		source_scheme_path = os.path.join(plugin_dir, 'Default.sublime-color-scheme')
		l_debug("copying '{source}' to '{dest}'", source=source_scheme_path, dest=scheme_dest_path)
		shutil.copy(source_scheme_path, scheme_dest_path)

def l_debug(msg, **kwargs):
	l.debug(msg.format(**kwargs))

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
