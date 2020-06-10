import sublime
import sublime_plugin
import logging
import re
import uuid
import os
import shutil
from threading import Timer

DEFAULT_LOG_LEVEL = logging.DEBUG
l = logging.getLogger(__name__)

PLUGIN_KEY = 'ScopedQuickSelect'

ARG_NAME_TARGET_SCOPE = 'scope'

SCOPE_MARKERS_KEY = PLUGIN_KEY + 'scope_markers'

# TODO: If we made these "immutable" and/or kept copies of these
# per "edit" we could check the command_history and roll-back
# the whole state instead of trying to re-create it?
VIEW_DATA = {}

TEMP_VIEWS_SHOWING = set()

class IncrementalMatch:
	__slots__ = ["selected", "region"]

	def __init__(self, selected, region):
		self.selected = selected
		self.region = region

class LayoutInfo:
	__slots__ = [
		"tabs_visible",
		"original_is_scratch",
		"active_sheets",
		"original_layout",
		"original_sheets",
	]

	def __init__(self):
		self.tabs_visible = False
		self.original_is_scratch = False
		self.active_sheets = None
		self.original_layout = None
		self.original_sheets = None

class ViewData:
	__slots__ = [
		"original_cursor_location",
		"visited_matches",
		"wrapped",
		"pattern",
		"original_layout_info",
		"start_clone",
		"end_clone",
		"timer",
	]

	def __init__(self):
		self.original_cursor_location = None
		self.visited_matches = []
		self.wrapped = False
		self.pattern = None
		self.original_layout_info = None
		self.start_clone = None
		self.end_clone = None
		self.timer = None

class ScopedQuickSelect(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		scoped_quick_select(self, self.view, edit, args[ARG_NAME_TARGET_SCOPE])

class SetQuickSelectScope(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		token = edit.edit_token
		self.view.end_edit(edit)

		# NOTE: Make sure each selection counts as it's own undo
		# Replace the edit (by beginning a new edit with the original
		# edit's token, and generate an arg with a new uuid so it won't
		# be grouped together with any previous version)
		new_args = dict(args)
		new_args[uuid.uuid4().hex] = 1
		subedit = self.view.begin_edit(token, self.name(), new_args)
		try:
			set_quick_select_scope(self, self.view, edit, args[ARG_NAME_TARGET_SCOPE])
		finally:
			self.view.end_edit(subedit)

class ClearQuickSelectScope(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		clear_quick_select_scope(self, self.view, edit)

class IncrementalQuickSelect(sublime_plugin.TextCommand):
	def run(self, edit, **args):
		incremental_quick_select(self, self.view, edit, args["add"].casefold() == "True".casefold())

class DismissScopePreview(sublime_plugin.TextCommand):
	def run(self, eidt, **args):
		view = self.view
		if view.id() in TEMP_VIEWS_SHOWING:
			trigger_restore_original_layout(VIEW_DATA[view.id()], view)

def rowcol_one_based(view, position):
	rowcol_zero_based = view.rowcol(position)
	return (rowcol_zero_based[0] + 1, rowcol_zero_based[1] + 1)

# TODO: use `view.match_selector()` instead?
def has_comment_scope(scopes):
	return any(scope.split('.')[0] == "comment" for scope in scopes.split(' '))

def has_string_scope(scopes):
	return any(scope.split('.')[0] == "string" for scope in scopes.split(' '))

def get_quick_select_scope(view, first_sel, target_scope, repeat_count):
	scope_region = sublime.Region(0, 0)
	if (target_scope == "all"):
		scope_region = sublime.Region(0, view.size())
	elif (target_scope == "function"):
		# TODO: Support languages that don't use "meta" markup
		functions = view.find_by_selector("meta.function")
		methods = view.find_by_selector("meta.methods")
		current_point = first_sel.begin()
		matching_functions = [r for r in functions + methods if r.contains(current_point)]
		l_debug("matching regions: {matching_functions}", matching_functions=matching_functions)
		if any(matching_functions):
			scope_region = min(matching_functions, key=lambda x: x.size())
		else:
			view.window().status_message('No surrounding function could be found')
			scope_region = sublime.Region(0, 0)

	elif (target_scope == "parentheses"):
		scope_region = get_delimited_scope_region(view, first_sel, repeat_count, '(', ')', 'parenthesis')
	elif (target_scope == "selection"):
		# TODO: support multiple selections
		scope_region = first_sel
	elif (target_scope == "curly braces"):
		scope_region = get_delimited_scope_region(view, first_sel, repeat_count, '{', '}', 'curly brace')
	elif (target_scope == "square brackets"):
		scope_region = get_delimited_scope_region(view, first_sel, repeat_count, '[', ']', 'square bracket')
	elif (target_scope == "angle brackets"):
		scope_region = get_delimited_scope_region(view, first_sel, repeat_count, '<', '>', 'angle bracket')
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
		if ".block.begin." in cursor_scopes:
			num_blocks_of_cursor -= 1

		# Expand per the repeat count
		num_blocks_of_cursor = max(num_blocks_of_cursor - repeat_count, 0)

		# TODO: Other language "blocks"
		# NOTE: Python doesn't actually have "block" scopes, variables
		# are accessible from their definition until the end of the
		# function they are defined in. But it might still be useful
		# to implement this:
		#
		#  if x:                   #  if x:|
		#      scope               #      scope
		#      t|o         ->      #      to
		#      this        ->      #      this
		#  else                    #  |else
		#      other               #      other
		#
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

			search_end = block_start
			brace_scopes = view.scope_name(block_start)
			if has_comment_scope(brace_scopes):
				l.debug('commented open brace at ' + str(rowcol_one_based(view, block_start)))
				continue

			if has_string_scope(brace_scopes):
				l.debug('string open brace at ' + str(rowcol_one_based(view, block_start)))
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
				l.debug('commented close brace at ' + str(rowcol_one_based(view, block_start)))
				continue

			if has_string_scope(brace_scopes):
				l.debug('string close brace at ' + str(rowcol_one_based(view, block_start)))
				continue

			num_blocks_of_brace = brace_scopes.count("meta.block")
			if num_blocks_of_brace == num_blocks_of_cursor:
				l.debug('found end brace: ' + str(block_end))
				break

		scope_region = sublime.Region(block_start, block_end)
	elif (target_scope == "current_marked_scope"):
		scope_region = get_marked_scope_region(view)
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

def set_tabs_visible_in_place(view, visible):
	"""Hide tabs without moving the viewport (physically on the screen)"""

	# TODO: If sublime ever allows us to set tabs visible per view/group
	# instead of per window, then we only need to show/hide the tabs for
	# the clone views and hopefully wouldn't have to worry about the
	# position.
	window = view.window()
	(orig_x, orig_y) = view.viewport_position()
	(origin_w, orig_h) = view.viewport_extent()
	window.set_tabs_visible(visible)
	(new_w, new_h) = view.viewport_extent()
	y_diff = new_h - orig_h
	new_position = (orig_x, orig_y - y_diff)
	# NOTE: Doesn't work correctly at the very top of the buffer
	view.set_viewport_position(new_position, False)

def restore_original_layout(view_data, view):
	window = view.window()
	layout_info = view_data.original_layout_info
	if not view_data.start_clone:
		return

	view.set_scratch(True)
	view_data.start_clone.close()
	view_data.end_clone.close()
	view.set_scratch(layout_info.original_is_scratch)

	window.set_layout(layout_info.original_layout)
	for (sheet, (group, index)) in layout_info.original_sheets:
		window.set_sheet_index(sheet, group, index)

	for sheet in layout_info.active_sheets:
		if sheet is not None:
			window.focus_sheet(sheet)

	# NOTE: If there's an empty group before the layout change
	# it seems that _something_ is causing it to always be
	# focused after the layout is restored
	# TODO: investigate what is causing this and if this deferral
	# is necessary or just a lazy hack
	sublime.set_timeout(lambda: window.focus_view(view), 0)

	if layout_info.tabs_visible:
		set_tabs_visible_in_place(view, True)

	TEMP_VIEWS_SHOWING.discard(view.id())
	view_data.original_layout_info = None
	view_data.start_clone = None
	view_data.end_clone = None

def trigger_restore_original_layout(view_data, original_view):
	"""This version of the function is just so we can funnel all of the calls
	   onto the main thread.

	   By running it on the main thread, we don't have to worry about trying
	   to keep the original view focused at all times (or any other thread-safety
	   shenanigans), we just need to make sure we leave the correct view focused
	   at the end."""
	sublime.set_timeout(lambda: restore_original_layout(view_data, original_view), 0)

def mark_in_view(view, location):
	view.add_regions(
	    SCOPE_MARKERS_KEY,
	    [sublime.Region(location, location)],
	    'scoped_quick_select.scope_marker',
	    flags=sublime.DRAW_EMPTY
	)
	view.show_at_center(location)

def register_temp_views_for_closure(view):
	TEMP_VIEWS_SHOWING.add(view.id())

def show_start_and_end_in_other_pane(view, view_data, scope_region):
	# Debounce the timer
	if view_data.timer is not None:
		view_data.timer.cancel()

	window = view.window()
	l.debug('show_start_and_end')

	# NOTE: This is extremely simplified, but I don't particularly
	# want to deal with every crazy combination of layouts... hopefully
	# this should suffice in the general case. (Readdress as necessary).
	XMIN, YMIN, XMAX, YMAX = list(range(4))
	active_view_on_lhs = True
	current_layout = window.get_layout()
	current_cell = current_layout["cells"][window.active_group()]
	if (current_cell[XMIN] > 0):
		active_view_on_lhs = False

	is_first_show = False
	if view_data.original_layout_info is None:
		original_layout_info = LayoutInfo()
		original_layout_info.tabs_visible = window.get_tabs_visible()
		original_layout_info.original_is_scratch = view.is_scratch()
		original_layout_info.active_sheets = [window.active_sheet_in_group(group) for group in range(0, window.num_groups())]
		original_layout_info.original_layout = window.get_layout()
		original_layout_info.original_sheets = [(sheet, window.get_sheet_index(sheet)) for sheet in window.sheets()]

		if not view.visible_region().contains(scope_region):
			view_data.original_layout_info = original_layout_info
			is_first_show = True

	if is_first_show:
		if active_view_on_lhs:
			view_group = 0
			start_group = 1
			end_group = 2
			window.set_layout({
				"cols": [0.0, 0.5, 1.0],
				"rows": [0.0, 0.5, 1.0],
				"cells": [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]
			})
		else:
			start_group = 0
			end_group = 1
			view_group = 2
			window.set_layout({
				"cols": [0.0, 0.5, 1.0],
				"rows": [0.0, 0.5, 1.0],
				"cells": [[0, 0, 1, 1], [0, 1, 1, 2], [1, 0, 2, 2]]
			})

		if view_data.start_clone is None:
			window.run_command('clone_file')
			window.run_command('move_to_group', {'group': start_group})
			view_data.start_clone = window.active_view()

		if view_data.end_clone is None:
			window.run_command('clone_file')
			window.run_command('move_to_group', {'group': end_group})
			view_data.end_clone = window.active_view()

		view_data.start_clone.sel().add_all(view.sel())
		view_data.end_clone.sel().add_all(view.sel())

		window.focus_view(view)
		window.run_command('move_to_group', {'group': view_group})

	if view_data.original_layout_info is not None:
		# NOTE: Make sure the view exists and is properly initialized before
		# we try to jump to the right position, otherwise it seems to get
		# quite confused.
		# TODO: Make this more robust... I think we can mark it immediately,
		# but jumping to the position definitely needs the view to already
		# know what the visible region is/will be. I don't know if there's
		# an event/callback we can hook into for that.
		sublime.set_timeout(lambda: mark_in_view(view_data.start_clone, scope_region.begin()), 50)
		sublime.set_timeout(lambda: mark_in_view(view_data.end_clone, scope_region.end()), 50)

	if is_first_show:
		if original_layout_info.tabs_visible:
			set_tabs_visible_in_place(view, False)

		sublime.set_timeout(lambda: register_temp_views_for_closure(view), 50)

	# Auto hide after timeout
	# NOTE: This is currently disabled, because I think it's better to let
	# the user scroll around in the begining/end clones if they need to
	# (for as long as they need to)
	#restore_layout_timeout_in_seconds = 2
	#view_data.timer = Timer(restore_layout_timeout_in_seconds, trigger_restore_original_layout, [view_data, view])
	#view_data.timer.start()

def set_quick_select_scope(text_command, view, edit, target_scope):
	l_debug('view {view_id} set_quick_select_scope({target_scope})',
	        view_id = view.id(), target_scope = target_scope)

	command_index = 0
	repeat_count = 0
	while True:
		(previous_command, previous_args, previous_repeat_count) = view.command_history(command_index)
		#l.debug((previous_command, previous_args, previous_repeat_count))

		if (previous_command == text_command.name() and
		    previous_args and
		    ARG_NAME_TARGET_SCOPE in previous_args and
		    previous_args[ARG_NAME_TARGET_SCOPE] == target_scope):

			repeat_count += previous_repeat_count
			command_index -= 1
		else:
			break

	l_debug('repeat_count: {repeat_count}', repeat_count=repeat_count)

	all_sel = view.sel()
	first_sel = all_sel[0];

	scope_region = get_quick_select_scope(view, first_sel, target_scope, repeat_count)
	key = view.id()

	view_data = VIEW_DATA.setdefault(key, ViewData())
	view_data.visited_matches = []
	view_data.pattern = None
	view_data.wrapped = False
	view_data.original_cursor_location = None

	if (scope_region.empty()):
		if (repeat_count > 0):
			l.debug('Kept original scope for view ' + str(key))
		else:
			VIEW_DATA[key] = ViewData()
			l.debug('Cleared scope for view ' + str(key))
			view.erase_regions(SCOPE_MARKERS_KEY)
	else:
		l_debug('Set scope {start} to {end}',
				start=rowcol_one_based(view, scope_region.begin()),
				end=view.rowcol(scope_region.end()))

		scope_markers = [sublime.Region(scope_region.begin(), scope_region.begin()),
						 sublime.Region(scope_region.end(), scope_region.end())]

		view.add_regions(SCOPE_MARKERS_KEY, scope_markers,
		                 'scoped_quick_select.scope_marker',
		                 flags=sublime.DRAW_EMPTY)

		# It's redundant to show the start/end if it's based on the selection
		# from a user; they should already know the extent of the scope.
		if (target_scope != 'selection'):
			show_start_and_end_in_other_pane(view, view_data, scope_region)

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

			if not view_data.pattern or not keep_original_pattern:
				view_data.pattern = get_pattern_for_selection(view, original_selection)

			if original_selection.size() < 1:
				word_region = view.word(original_selection)
				view_data.original_cursor_location = word_region.begin()
				most_recent_cursor_location = word_region.begin()
			else:
				view_data.original_cursor_location = original_selection.begin()
				most_recent_cursor_location = original_selection.end()
				view_data.visited_matches.append(IncrementalMatch(True, original_selection))
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
			l.debug('unmatched pattern: ' + view_data.pattern)
			del VIEW_DATA[view.id()]
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

def get_delimited_scope_region(view, original_selection, repeat_count, open_delim, close_delim, name):
	search_end = original_selection.begin()
	open_match_count = 0
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
			if (not has_comment_scope(other_delim_scopes) and
				not has_string_scope(other_delim_scopes)):
				num_unmatched_delimiters += 1
			search_end = other_block_end - close_delim_len + 1
			continue

		search_end = block_start
		delim_scopes = view.scope_name(block_start)
		# TODO: Allow scoping to delimiters inside comments? (e.g. like this)
		# I think I want this to be scoped to a single comment "block"
		# which means consecutive single line comments, or a single
		# block comment for languages that support them
		if has_comment_scope(delim_scopes):
			l.debug('commented open ' + name + ' at ' + str(rowcol_one_based(view, block_start)))
			continue

		if has_string_scope(delim_scopes):
			l.debug('string open ' + name + ' at ' + str(rowcol_one_based(view, block_start)))
			continue

		if num_unmatched_delimiters == 0:
			l.debug('match open ' + name + ' at ' + str(rowcol_one_based(view, block_start)))
			if open_match_count >= repeat_count:
				break
			open_match_count += 1
		else:
			num_unmatched_delimiters -= 1


	search_start = original_selection.end();
	close_match_count = 0
	block_end = search_start;
	view_end = view.size()
	while search_start < view_end:
		# l_debug('Searching for close {name} between {start} to {end}',
		# 		name=name,
		# 		start=rowcol_one_based(view, search_start),
		# 		end=rowcol_one_based(view, view_end))

		# TODO: This is probably going to be a perf bottleneck
		text_after = view.substr(sublime.Region(search_start, view_end))
		block_end = text_after.find(close_delim)
		other_block_start = text_after.find(open_delim)

		if block_end < 0:
			l.debug('reached end of buffer')
			view.window().status_message('No matching close ' + name)
			return sublime.Region(0, 0)

		block_end += search_start

		if other_block_start > -1 and other_block_start < block_end:
			other_block_start += search_start
			if other_block_start < block_end:
				other_delim_scopes = view.scope_name(other_block_start)
				if (not has_comment_scope(other_delim_scopes) and
					not has_string_scope(other_delim_scopes)):
					num_unmatched_delimiters += 1
					#l_debug('Found open {name} at {position}', name=name, position=rowcol_one_based(view., other_block_start))
				#else:
				#	l_debug('Found commented open {name} at {position}', name=name, position=rowcol_one_based(view., other_block_start))
				search_start = other_block_start + open_delim_len
				continue

		search_start = block_end + close_delim_len

		delim_scopes = view.scope_name(block_end)
		if has_comment_scope(delim_scopes):
			l.debug('commented closed ' + name + ' at ' + str(rowcol_one_based(view, block_end)))
			continue

		if has_string_scope(delim_scopes):
			l.debug('string closed ' + name + ' at ' + str(rowcol_one_based(view, block_end)))
			continue

		if num_unmatched_delimiters == 0:
			l.debug('match close ' + name + ' at ' + str(rowcol_one_based(view, block_end)))
			if close_match_count >= repeat_count:
				break
			close_match_count += 1
		else:
			num_unmatched_delimiters -= 1

	block_start += open_delim_len
	l_debug('{name} scope bounds: {start} to {end}',
			name  = name,
			start = rowcol_one_based(view, block_start),
			end   = rowcol_one_based(view, block_end))

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

	scope_region = get_quick_select_scope(view, selection, target_scope, 0)
	if scope_region.empty():
		scoped_matches = []
	else:
		scoped_matches = [scope_region.intersection(m) for m in matches]

	scoped_matches = [m for m in scoped_matches if not m.empty()]

	if any(scoped_matches):
		view_data = VIEW_DATA.setdefault(view.id(), ViewData())
		all_sel.add_all(scoped_matches)
		show_start_and_end_in_other_pane(view, view_data, scope_region)

class ScopedQuickSelectListener(sublime_plugin.EventListener):
	registered_views = set()
	color_schemes = set()

	def __init__(self):
		# NOTE: Clear all scopes from previous sessions
		for window in sublime.windows():
			for view in window.views():
				view.erase_regions(SCOPE_MARKERS_KEY)

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
		self.registered_views.discard(view.id())

	def on_load_async(self, view):
		pass

	def on_modified(self, view):
		#l_debug('on_modified {view}', view = view)
		if view.id() in TEMP_VIEWS_SHOWING:
			trigger_restore_original_layout(VIEW_DATA[view.id()], view)

	def on_text_command(self, view, command_name, args):
		#l_debug('on_text_command {view}, {command_name}, {args}',
		#        view = view, command_name = command_name, args = args)

		if command_name != 'set_quick_select_scope':
			if view.id() in TEMP_VIEWS_SHOWING:
				trigger_restore_original_layout(VIEW_DATA[view.id()], view)

		return None

	def on_query_context(self, view, key, operator, operand, match_all):
		def test(a):
			if operator == sublime.OP_EQUAL:
				return a == operand
			if operator == sublime.OP_NOT_EQUAL:
				return a != operand
			return False

		if key == "scoped_quick_select_preview_showing":
			return test(view.id() in TEMP_VIEWS_SHOWING)

		return None

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
