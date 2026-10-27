# Tasks backend

We need a small backend for a personal task tracker.

People sign up with an email and a password and log in to get a token. Everything else needs that
token.

Each user keeps their own **projects**. A project has a name (1 to 200 characters) and an archived
flag that starts as false. Projects are private: nobody else can see, change or delete them, and
other users get "not found". A user may have at most 20 projects that are not archived; trying to
create the 21st fails with a conflict (409). Archiving is a separate action on a project. Archiving
a project that is already archived is also a conflict (409).

Each project has **tasks**. A task has a title (1 to 200 characters), an optional due date and a
status of todo, doing or done. New tasks start as todo. A task's status only moves forward, from
todo to doing and from doing to done; any other change is a conflict (409). A project that is
archived does not accept new tasks (409). Deleting a project deletes its tasks.

Lists of projects and tasks are paged.
