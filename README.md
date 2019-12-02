# Scoped Quick Select

Rough version of Sublime's native "find_under_expand" but limited to a
particular pre-marked "scope", e.g. inside a block / function.

Mark a scope with `alt + s`, `<scope key>`

 | Shortcut     | Scope                              |
 |--------------|------------------------------------|
 | `alt+s`, `f` | function                           |
 | `alt+s`, `b` | block                              |
 | `alt+s`, `s` | current selection                  |
 | `alt+s`, `/` | comment                            |
 | `alt+s`, `(` | parentheses                        |
 | `alt+s`, `)` | parentheses                        |
 | `alt+s`, `{` | curly braces                       |
 | `alt+s`, `}` | curly braces                       |
 | `alt+s`, `[` | square brackets                    |
 | `alt+s`, `]` | square brackets                    |
 | `alt+s`, `<` | angle brackets                     |
 | `alt+s`, `>` | angle brackets                     |
 | `alt+s`, `'` | single quoted string               |
 | `alt+s`, `"` | double quoted string               |
 | `alt+s`, \`  | backtick quoted string             |

Double tap `alt + s` to clear the currently marked scope

## This is still very much a WIP

"function" and "block" scopes are still in the early stages.
These are intended to be (relatively) language/syntax aware,
similar to an IDE refactor command.

Quoted strings selection also hasn't been implemented yet.
