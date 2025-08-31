#
# SPDX-FileCopyrightText: 2023 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import logging
from os import getenv, path

from langchain_community.llms.ctransformers import CTransformers

logger = logging.getLogger('ccb.models')

def get_model_for(model_type: str, model_config: dict):
	model_dir = getenv('MODEL_DIR', 'persistent_storage/model_files')
	if str(model_config.get('model')).startswith('/'):
		model_dir = ''

	model_path = path.join(model_dir, model_config.get('model', ''))
	logger.info(f'Loading ctransformers model from {model_path}')

	if model_config is None:
		return None

	if model_type == 'llm':
		return CTransformers(**{**model_config, 'model': model_path})

	return None
