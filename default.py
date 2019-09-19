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

# TODO: If we made these "immutable" and/or kept copies of these
# per "edit" we could check the command_history and roll-back
# the whole state instead of trying to re-create it?
VIEW_DATA = {}

class IncrementalMatch:
	__slots__ = ["selected", "region"]

	def __init__(self, selected, region):
		self.selected = selected
		self.region = region

class ViewData:
	__slots__ = [
		"original_cursor_location",
		"visited_matches",
		"wrapped",
		"pattern",
	]

	def __init__(self):
		self.original_cursor_location = None
		self.visited_matches = []
		self.wrapped = False
		self.pattern = None

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
		incremental_quick_select(self, self.view, edit, args["add"].casefold() == "True".casefold())

# TODO: use `view.match_selector()` instead?
def has_comment_scope(scopes):
	return any(scope.split('.')[0] == "comment" for scope in scopes.split(' '))

def get_quick_select_scope(view, first_sel, target_scope):
	# TODO: multiple calls in a row to expand the scope out
	# by one level (e.g. out one block, or pair of matched delimiters)
	scope_region = sublime.Region(0, 0)
	if (target_scope == "all"):
		scope_region = sublime.Region(0, view.size())
	elif (target_scope == "function"):
		l.warn('TODO: implement')
	elif (target_scope == "parentheses"):
		scope_region = get_delimited_scope_region(view, first_sel, '(', ')', 'parenthesis')
	elif (target_scope == "selection"):
		# TODO: support multiple selections
		scope_region = first_sel
	elif (target_scope == "curly braces"):
		scope_region = get_delimited_scope_region(view, first_sel, '{', '}', 'curly brace')
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
	elif (target_scope == "backticks"):
		# TODO: Need to be careful about escaped backticks here
		l.warn('TODO: implement')
	elif (target_scope == "block"):
		cursor_scopes = view.scope_name(first_sel.begin())
		num_blocks_of_cursor = cursor_scopes.count("meta.block")

		# TODO: Other language "blocks"
		search_end = first_sel.begin();
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

		search_start = first_sel.end();
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
	if key in VIEW_DATA:
		VIEW_DATA[key] = ViewData()
		l_debug('Cleared scope for view ' + str(key))

def set_quick_select_scope(text_command, view, edit, target_scope):
	l_debug('view {view_id} set_quick_select_scope({target_scope})',
	        view_id = view.id(), target_scope = target_scope)
	all_sel = view.sel()
	first_sel = all_sel[0];

	scope_region = get_quick_select_scope(view, first_sel, target_scope)
	key = view.id()

	view_data = VIEW_DATA.setdefault(key, ViewData())
	view_data.visited_matches = []
	view_data.pattern = None
	view_data.wrapped = False
	view_data.original_cursor_location = None

	if (scope_region.empty()):
		VIEW_DATA[key] = ViewData()
		l.debug('Cleared scope for view ' + str(key))
		view.erase_regions(SCOPE_MARKERS_KEY)
	else:
		l_debug('Set scope {start} to {end}',
				start=view.rowcol(scope_region.begin()),
				end=view.rowcol(scope_region.end()))

		scope_markers = [sublime.Region(scope_region.begin(), scope_region.begin()),
						 sublime.Region(scope_region.end(), scope_region.end())]

		view.add_regions(SCOPE_MARKERS_KEY, scope_markers,
		                 'scoped_quick_select.scope_marker',
		                 flags=sublime.DRAW_EMPTY)

		view.show(scope_region.end())

def get_marked_scope_region(view):
	marked_regions = view.get_regions(SCOPE_MARKERS_KEY)
	if len(marked_regions) < 2:
		# Clean up any single dangling region
		view.erase_regions(SCOPE_MARKERS_KEY)
		return None

	start = marked_regions[0]
	end = marked_regions[-1]
	return sublime.Region(start.begin(), end.end())

def incremental_quick_select(text_command, view, edit, add):
	l_debug('view {view_id} incremental_quick_select({add})',
	        view_id = view.id(), add = add)

	view_data = VIEW_DATA.setdefault(view.id(), ViewData())

	external_selection_change = False
	for visited in view_data.visited_matches:
		if not (view.sel().contains(visited.region) == visited.selected):
			external_selection_change = True
			break

	keep_original_pattern = False
	undo_count = 0
	if external_selection_change:
		l.debug('selection changed!')
		redo_index = 1

		(most_recent_command, _, _) = view.command_history(0)

		# NOTE: This is definitely not how you're supposed to handle this
		# but it works for the simple case of determining between the
		# selection changing because the user moved the cursor manually
		# and because they just did a "soft undo" (possibly repeatedly)
		if most_recent_command == incremental_quick_select.__name__:
			while True:
				command_history = view.command_history(redo_index)
				l.debug(str(command_history))
				(redo_command, redo_args, repetitions) = command_history
				redo_index += 1
				if repetitions == 0:
					break

				if redo_command == incremental_quick_select.__name__:
					undo_count += repetitions

		view_data.wrapped = False

		if undo_count == 0:
			view_data.visited_matches = []
			view_data.original_cursor_location = None
		else:
			keep_original_pattern = True
			for i in range(undo_count):
				if view_data.visited_matches:
					view_data.visited_matches.pop()


	# TODO: expand any single cursors to the surrounding words,
	# but then carry on as usual

	token = edit.edit_token

	# NOTE: make sure each selection counts as it's own undo
	view.end_edit(edit)
	subedit = view.begin_edit(token, text_command.name())
	try:
		scope_region = get_marked_scope_region(view)
		if scope_region is None:
			scope_region = sublime.Region(0, view.size())

		if (   view_data.original_cursor_location is None
			or view_data.pattern is None):
			original_selection = view.sel()[-1]

			if not keep_original_pattern:
				view_data.pattern = get_pattern_for_selection(view, original_selection)

			if original_selection.size() < 1:
				word_region = view.word(original_selection)
				view_data.original_cursor_location = word_region.begin()
				most_recent_cursor_location = word_region.begin()
			else:
				view_data.original_cursor_location = original_selection.begin()
				most_recent_cursor_location = original_selection.end()
		else:
			if view_data.visited_matches:
				most_recent_cursor_location = view_data.visited_matches[-1].region.end()
			else:
				most_recent_cursor_location = view_data.original_cursor_location

		next_match_no_wrap = view.find(view_data.pattern, most_recent_cursor_location)
		next_match = next_match_no_wrap
		if not scope_region.contains(next_match):
			# No match found between `most_recent_cursor_location`
			# and scope_region.end(), try from the start of the region
			next_match = view.find(view_data.pattern, scope_region.begin())
			view_data.wrapped = True

		if next_match_no_wrap.a == -1 and next_match.a == -1:
			view.window().status_message("Could not automatically match text at cursor")
			return

		if view_data.wrapped and next_match.begin() >= view_data.original_cursor_location:
			if not add:
				if view_data.visited_matches:
					previous_visit = view_data.visited_matches[-1]
					previous_visit.selected = False
					view.sel().subtract(previous_visit.region)
			view.window().status_message("Incremental select complete")
			return

		if add:
			view.sel().add(next_match)
		else:
			if view_data.visited_matches:
				previous_visit = view_data.visited_matches[-1]
				previous_visit.selected = False
				view.sel().subtract(previous_visit.region)
			view.sel().add(next_match)

		view_data.visited_matches.append(IncrementalMatch(True, next_match))
		view.show(next_match)
	finally:
		view.end_edit(subedit)

def get_delimited_scope_region(view, original_selection, open_delim, close_delim, name):
	search_end = original_selection.begin()
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


	search_start = original_selection.end();
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

def regex_escape(text):
	# NOTE: Sublime does not use python's regex engine so we can't just use
	# `regex_escape()` and have it work. Sources seem to suggest that it is
	# using the BOOST regex engine. So we need to escape the special characters
	#
	#    . ^ $ | ( ) [ ] { } * + ? \

	special_regex_chars = re.compile(r'([.^$|()\[\]{}*+?\\])')
	return special_regex_chars.sub(r'\\\1', text)

def get_pattern_for_selection(view, selection):
	if selection.size() < 1:
		word_around_cursor = view.substr(view.word(selection))
		regex = '\\b' + regex_escape(word_around_cursor) + '\\b'

		if l.isEnabledFor(logging.DEBUG):
			l_debug('just a cursor at {cursor_pos} inside the word `{word}`',
			        cursor_pos = view.rowcol(selection.begin()),
			        word = word_around_cursor)
	else:
		selected_text = view.substr(selection)
		regex = regex_escape(selected_text)

		if l.isEnabledFor(logging.DEBUG):
			l_debug('some text `{selected_text}`'
			        ' selected at {start_pos} to {end_pos}',
			        selected_text = selected_text,
			        start_pos = view.rowcol(selection.begin()),
			        end_pos = view.rowcol(selection.end()))

	return regex

def scoped_quick_select(text_command, view, edit, target_scope):
	l_debug('view {view_id} scoped_quick_select({target_scope})',
	        view_id = view.id(), target_scope = target_scope)
	all_sel = view.sel()
	selection = all_sel[0];

	regex = get_pattern_for_selection(view, selection)

	matches = view.find_all(regex)

	scope_region = get_quick_select_scope(view, selection, target_scope)
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
		view.erase_regions(SCOPE_MARKERS_KEY)

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
