/*
 * Copyright 2014 Google Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS-IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

window.ANDROID_CLIENT = (function() {

  var module = {}
  module._editorFile = null;
  module._projectName = null;
  module._statusUpdateIntervalId = null
  module._statusUpdateIntervalMsec = 50;
  module._statusUpdateIntervalStart = null;

  module._runPayload = null;
  module._runPollingIntervalId = null;
  module._runPollingIntervalMsec = 3000;
  module._runPollingTimeoutSec = 90;
  module._runStart = null;
  module._runTicket = null;

  module._taskComplete = "complete";
  module._taskRunning = "running";

  module.main = function(optProjectName) {
    var editor = ace.edit("editor");
    var projectName = optProjectName || module._getProjectNameFromUrl();
    var request = {
      request: JSON.stringify({
        project: projectName
      })
    };
    $.ajax({
      url: "/rest/balancer/v1/project",
      crossDomain: true,
      type: "GET",
      data: request,
      dataType: "json",
      success: function(data, success, xhr) {
        module._onProjectGetSuccess(editor, data, success, xhr);
      },
      error: module._onProjectGetError
    });
  };

  module._addResult = function(data) {
    module._getResultElement().append(data);
  };

  module._clearResult = function() {
    module._getResultElement().empty();
  };

  module._clearRun = function() {
    module._runPayload = null;
    module._runTicket = null;
  }

  module._configureEditor = function(editor) {
    editor.setFontSize(14);
    editor.setTheme("ace/theme/monokai");
    editor.getSession().setMode(module._getEditorMode());
  };

  module._getContentsElement = function() {
    return $("#contents");
  };

  module._getDeltaSeconds = function(from, to) {
    if (from === null) {
      return 0;
    }

    return Number((to.getTime() - from.getTime()) / 1000).toFixed(1);
  };

  module._getEditorMode = function() {
    var parts = ["text"];

    if (-1 != module._editorFile.indexOf(".")) {
      parts = module._editorFile.split(".");
    }

    return "ace/mode/" + parts[parts.length - 1];
  };

  module._getErrorElement = function() {
    return $("#error");
  };

  module._getFilename = function(filename, projectName) {
    var index = filename.indexOf(projectName);

    if (index == -1) {
      return null;
    }

    return filename.slice(index);
  };

  module._getFilenameElement = function() {
    return $("#filename");
  };

  module._getLoadingElement = function() {
    return $("#loading");
  };

  module._getProjectNameFromUrl = function() {
    return module._getUrlArgs().project;
  };

  module._getResultElement = function() {
    return $("#result");
  }

  module._getRunControlElement = function() {
    return $("#run-control");
  };

  module._getRunResults = function(ticket) {
    var delta = module._getDeltaSeconds(module._runStart, new Date());
    if (delta > module._runPollingTimeoutSec) {
      module._stopPolling();
      module._setUiStateRunDone("Ready", "Run timed out.")
      return;
    }

    var request = {
      request: JSON.stringify({
        ticket: ticket
      })
    };
    $.ajax({
      url: "/rest/balancer/v1",
      crossDomain: true,
      type: "GET",
      data: request,
      dataType: "json",
      success: module._onRunGetSuccess,
      error: module._onRunGetError
    });
  };

  module._onRunGetError = function(xhr, status, error) {
    module._stopPolling();
    module._clearRun();
    module._setUiStateRunDone(
      "Ready", "Error fetching results; please try again.");
  };

  module._onRunGetSuccess = function(data, success, xhr) {
    if (xhr.status != 200) {
      module._onRunGetError(xhr, status, data.payload);
      return;
    }

    if (data.payload && data.payload.status === module._taskRunning) {
      return;
    }

    module._stopPolling();
    module._clearRun();

    var payload = module._formatPayload(
      data.payload.status, data.payload.payload);
    var status = "Finished run of " + module._projectName + ". Status: " +
      data.payload.status + ". Result: ";
    module._setUiStateRunDone(status, payload);
  };

  module._getStatusElement = function() {
    return $("#status");
  }

  module._getUrlArgs = function() {
    var queries = {};
    $.each(window.location.search.substr(1).split("&"), function(c, query) {
      var i = query.split("=");
      queries[i[0].toString()] = i[1].toString();
    });
    return queries;
  };

  module._formatPayload = function(status, payload) {
    switch (status) {
      case module._taskComplete:
        return module._formatTaskCompletePayload(payload);
      default:
        return module._newlineToBr(payload);
    }
  };

  module._formatTaskCompletePayload = function(payload) {
    return "<img src='data:image/jpeg;base64," + payload + "' />";
  };

  module._makePatch = function(filename, contents) {
    return {
      contents: contents,
      filename: filename
    }
  }

  module._newlineToBr = function(data) {
    return (data + '').replace(
      /([^>\r\n]?)(\r\n|\n\r|\r|\n)/g, "$1" + "<br />" + "$2");
  };

  module._onProjectGetError = function(xhr, status, error) {
    module._setError(
      $("<p>Unable to get application data. <a href='" +
        window.location.href + "'>Try again?" +
        "</a>"));
    module._setUiStateInitialLoadFailed();
  };

  module._onProjectGetSuccess = function(editor, data, status, xhr) {
    if (xhr.status != 200) {
      module._onProjectGetError(xhr, status, data.payload);
      return;
    }

    module._editorFile = data.payload.filename;
    module._projectName = data.payload.projectName;
    module._setFilename(
      module._getFilename(module._editorFile, module._projectName));
    module._configureEditor(editor);
    module._setEditorContents(editor, data.payload.contents);
    module._setUiStateReady();
  };

  module._onProjectPostError = function(xhr, status, error) {
    var payload = (xhr.responseText && xhr.responseJSON === "Worker locked") ?
      "All workers busy; please try again later." :
      "Unable to start job.";
    module._setUiStateRunDone("Ready", payload);
  };

  module._onProjectPostSuccess = function(data, status, xhr) {
    if (xhr.status != 200) {
      module._onProjectPostError(xhr, status, data.payload);
      return;
    };

    module._startPolling(data.payload.ticket);
  };

  module._onRunControlClick = function(event) {
    var editor = ace.edit("editor");
    module._setUiStateRunning();
    var request = {
      request: JSON.stringify({
        patches: [module._makePatch(module._editorFile, editor.getValue())],
        project: module._projectName,
        user_id: 'some_user'
      })
    };
    $.ajax({
      url: "/rest/balancer/v1",
      crossDomain: true,
      type: "POST",
      data: request,
      dataType: "json",
      success: module._onProjectPostSuccess,
      error: module._onProjectPostError
    });
  };

  module._setEditorContents = function(editor, data) {
    editor.getSession().setValue(data);
  };

  module._setError = function(data) {
    module._getErrorElement().empty().append(data);
  };

  module._setFilename = function(data) {
    module._getFilenameElement().text(data);
  };

  module._setStatus = function(data) {
    module._getStatusElement().text(data);
  };

  module._setUiStateInitialLoadFailed = function() {
    module._getRunControlElement().off("click");
    module._getContentsElement().hide();
    module._getErrorElement().show();
    module._getLoadingElement().hide();
  };

  module._setUiStateReady = function() {
    module._getRunControlElement().on("click", module._onRunControlClick);
    module._getContentsElement().show();
    module._getErrorElement().hide()
    module._getLoadingElement().hide();
  };

  module._setUiStateRunning = function() {
    var control = module._getRunControlElement();
    control.removeClass("glyphicon-play");
    control.addClass("control-running");
    control.addClass("glyphicon-repeat");
    module._getRunControlElement().off("click");
    module._clearResult();
    module._startStatusUpdates();
  };

  module._setUiStateRunDone = function(status, optPayload) {
    module._stopStatusUpdates();

    var control = module._getRunControlElement();

    control.addClass("glyphicon-play");
    control.removeClass("control-running");
    control.removeClass("glyphicon-repeat");
    module._getRunControlElement().on("click", module._onRunControlClick);
    module._clearResult();
    module._setStatus(status);

    if (optPayload !== undefined) {
      module._addResult(optPayload + "<br />");
    }
  };

  module._startPolling = function(ticket) {
    if (module._runPollingIntervalId !== null) {
      console.log("non-null polling id");
      return;
    }

    module._runStart = new Date();
    module._runPollingIntervalId = window.setInterval(function() {
      module._getRunResults(ticket);
    }, module._runPollingIntervalMsec);
  };

  module._stopPolling = function() {
    window.clearTimeout(module._runPollingIntervalId);
    module._runPollingIntervalId = null;
    module._runStart = null;
  };

  module._startStatusUpdates = function() {
    if (module._statusUpdateIntervalId !== null) {
      return;
    }
    
    module._statusUpdateIntervalStart = new Date();

    var prefix = "Started run of " + module._projectName;
    var suffix = " (0.0s)"
    module._setStatus(prefix + suffix);
    module._statusUpdateIntervalId = window.setInterval(function () {
      suffix = " (" + module._getDeltaSeconds(
        module._statusUpdateIntervalStart, new Date()) + "s)";
      module._setStatus(prefix + suffix);
    }, module._statusUpdateIntervalMsec);
  };

  module._stopStatusUpdates = function() {
    window.clearTimeout(module._statusUpdateIntervalId);
    module._statusUpdateIntervalId = null
    module._statusUpdateIntervalStart = null;
  };

  return module;
})(jQuery, ace);
