# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from typing import Optional
from google.cloud.aiplatform import aiplatform


class _Config:
    """Store common configurations and current workflow for remote execution."""

    def __init__(self):
        self._remote = False
        # TODO(b/271613069) self._workflow = ...

    def init(self, *, remote: Optional[bool] = False, **kwargs):
        if remote is not None:
            self._remote = remote
        aiplatform.init(**kwargs)

    @property
    def remote(self):
        return self._remote

    def __getattr__(self, name):
        return getattr(aiplatform.initializer.global_config, name)


global_config = _Config()