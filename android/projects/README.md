# fe

To add a project, create a new directory here with its contents. Your project
must be buildable via gradle. See the existing projects (Example and Sample) for
examples.

For each project you want the worker to be able to process, create an entry in
`config.json` of the form

```json
"Name": {
  "editorFile": "relative/path/to/file/to/show/in/editor",
  "package": "your.package",
  "testClass": "your.package.TestClass",
  "testPackage": "your.package.tests"
}
```

where Name is the name of the folder in `projects/` and the other fields are as
described above. See `config.json` for examples. You probably want to remove the
example projects from `config.json` in your deployments.
