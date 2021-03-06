# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""The page cycler measurement.

This measurement registers a window load handler in which is forces a layout and
then records the value of performance.now(). This call to now() measures the
time from navigationStart (immediately after the previous page's beforeunload
event) until after the layout in the page's load event. In addition, two garbage
collections are performed in between the page loads (in the beforeunload event).
This extra garbage collection time is not included in the measurement times.

Finally, various memory and IO statistics are gathered at the very end of
cycling all pages.
"""

import collections
import os

from metrics import cpu
from metrics import io
from metrics import memory
from metrics import speedindex
from metrics import v8_object_stats
from telemetry.core import util
from telemetry.page import page_measurement

class PageCycler(page_measurement.PageMeasurement):
  def __init__(self, *args, **kwargs):
    super(PageCycler, self).__init__(*args, **kwargs)

    with open(os.path.join(os.path.dirname(__file__),
                           'page_cycler.js'), 'r') as f:
      self._page_cycler_js = f.read()

    self._record_v8_object_stats = False
    self._report_speed_index = False
    self._speedindex_metric = speedindex.SpeedIndexMetric()
    self._memory_metric = None
    self._cpu_metric = None
    self._v8_object_stats_metric = None
    self._cold_run_start_index = None
    self._has_loaded_page = collections.defaultdict(int)

  def AddCommandLineOptions(self, parser):
    # The page cyclers should default to 10 iterations. In order to change the
    # default of an option, we must remove and re-add it.
    # TODO: Remove this after transition to run_benchmark.
    pageset_repeat_option = parser.get_option('--pageset-repeat')
    pageset_repeat_option.default = 10
    parser.remove_option('--pageset-repeat')
    parser.add_option(pageset_repeat_option)

    parser.add_option('--v8-object-stats',
        action='store_true',
        help='Enable detailed V8 object statistics.')

    parser.add_option('--report-speed-index',
        action='store_true',
        help='Enable the speed index metric.')

    parser.add_option('--cold-load-percent', type='int',
                      help='%d of page visits for which a cold load is forced')

  def DidStartBrowser(self, browser):
    """Initialize metrics once right after the browser has been launched."""
    self._memory_metric = memory.MemoryMetric(browser)
    self._cpu_metric = cpu.CpuMetric(browser)
    if self._record_v8_object_stats:
      self._v8_object_stats_metric = v8_object_stats.V8ObjectStatsMetric()

  def DidStartHTTPServer(self, tab):
    # Avoid paying for a cross-renderer navigation on the first page on legacy
    # page cyclers which use the filesystem.
    tab.Navigate(tab.browser.http_server.UrlOf('nonexistent.html'))

  def WillNavigateToPage(self, page, tab):
    page.script_to_evaluate_on_commit = self._page_cycler_js
    if self.ShouldRunCold(page.url):
      tab.ClearCache()
    if self._report_speed_index:
      self._speedindex_metric.Start(page, tab)

  def DidNavigateToPage(self, page, tab):
    self._memory_metric.Start(page, tab)
    # TODO(qyearsley): Uncomment the following line and move it to
    # WillNavigateToPage once the cpu metric has been changed.
    # This is being temporarily commented out to let the page cycler
    # results return to how they were before the cpu metric was added.
    # self._cpu_metric.Start(page, tab) See crbug.com/301714.
    if self._record_v8_object_stats:
      self._v8_object_stats_metric.Start(page, tab)

  def CustomizeBrowserOptions(self, options):
    memory.MemoryMetric.CustomizeBrowserOptions(options)
    io.IOMetric.CustomizeBrowserOptions(options)
    options.AppendExtraBrowserArgs('--js-flags=--expose_gc')

    if options.v8_object_stats:
      self._record_v8_object_stats = True
      v8_object_stats.V8ObjectStatsMetric.CustomizeBrowserOptions(options)

    if options.report_speed_index:
      self._report_speed_index = True

    cold_runs_percent_set = (options.cold_load_percent != None)
    # Handle requests for cold cache runs
    if (cold_runs_percent_set and
        (options.repeat_options.page_repeat_secs or
         options.repeat_options.pageset_repeat_secs)):
      raise Exception('--cold-load-percent is incompatible with timed repeat')

    if (cold_runs_percent_set and
        (options.cold_load_percent < 0 or options.cold_load_percent > 100)):
      raise Exception('--cold-load-percent must be in the range [0-100]')

    # Make sure _cold_run_start_index is an integer multiple of page_repeat.
    # Without this, --pageset_shuffle + --page_repeat could lead to
    # assertion failures on _started_warm in WillNavigateToPage.
    if cold_runs_percent_set:
      number_warm_pageset_runs = int(
          (int(options.repeat_options.pageset_repeat_iters) - 1) *
          (100 - options.cold_load_percent) / 100)
      number_warm_runs = (number_warm_pageset_runs *
                          options.repeat_options.page_repeat_iters)
      self._cold_run_start_index = (number_warm_runs +
          options.repeat_options.page_repeat_iters)
      self.discard_first_result = (not options.cold_load_percent or
                                   self.discard_first_result)
    else:
      self._cold_run_start_index = (
          options.repeat_options.pageset_repeat_iters *
          options.repeat_options.page_repeat_iters)

  def MeasurePage(self, page, tab, results):
    tab.WaitForJavaScriptExpression('__pc_load_time', 60)

    chart_name_prefix = ('cold_' if self.IsRunCold(page.url) else
                         'warm_')

    results.Add('page_load_time', 'ms',
                int(float(tab.EvaluateJavaScript('__pc_load_time'))),
                chart_name=chart_name_prefix+'times')

    self._has_loaded_page[page.url] += 1

    self._memory_metric.Stop(page, tab)
    self._memory_metric.AddResults(tab, results)
    # TODO(qyearsley): Uncomment the following line when CPU metric is
    # changed. See crbug.com/301714.
    # self._cpu_metric.Stop(page, tab)
    # self._cpu_metric.AddResults(tab, results)
    if self._record_v8_object_stats:
      self._v8_object_stats_metric.Stop(page, tab)
      self._v8_object_stats_metric.AddResults(tab, results)

    if self._report_speed_index:
      def SpeedIndexIsFinished():
        return self._speedindex_metric.IsFinished(tab)
      util.WaitFor(SpeedIndexIsFinished, 60)
      self._speedindex_metric.Stop(page, tab)
      self._speedindex_metric.AddResults(
          tab, results, chart_name=chart_name_prefix+'speed_index')

  def DidRunTest(self, browser, results):
    self._memory_metric.AddSummaryResults(results)
    io.IOMetric().AddSummaryResults(browser, results)

  def IsRunCold(self, url):
    return (self.ShouldRunCold(url) or
            self._has_loaded_page[url] == 0)

  def ShouldRunCold(self, url):
    # We do the warm runs first for two reasons.  The first is so we can
    # preserve any initial profile cache for as long as possible.
    # The second is that, if we did cold runs first, we'd have a transition
    # page set during which we wanted the run for each URL to both
    # contribute to the cold data and warm the catch for the following
    # warm run, and clearing the cache before the load of the following
    # URL would eliminate the intended warmup for the previous URL.
    return (self._has_loaded_page[url] >= self._cold_run_start_index)

  def results_are_the_same_on_every_page(self):
    return False
