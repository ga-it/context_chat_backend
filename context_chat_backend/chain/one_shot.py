#
# SPDX-FileCopyrightText: 2023 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import logging

from langchain.llms.base import LLM

from ..dyn_loader import VectorDBLoader
from ..types import TConfig
from .context import get_context_chunks, get_context_docs
from .query_proc import get_pruned_query
from .types import ContextException, LLMOutput, ScopeType

_LLM_TEMPLATE = '''Answer based only on this context and do not add any imaginative details. Make sure to use the same language as the question in your answer.
{context}

{question}
''' # noqa: E501

logger = logging.getLogger('ccb.chain')

def process_query(
	user_id: str,
	llm: LLM,
	app_config: TConfig,
	query: str,
	no_ctx_template: str | None = None,
	end_separator: str = '',
):
	"""
	Raises
	------
	ValueError
		If the context length is too small to fit the query
	"""
	stop = [end_separator] if end_separator else None
	output = llm.invoke(
		(query, get_pruned_query(llm, app_config, query, no_ctx_template, []))[no_ctx_template is not None],  # pyright: ignore[reportArgumentType]
		stop=stop,
		userid=user_id,
	).strip()

	return LLMOutput(output=output, sources=[])


def process_context_query(
	user_id: str,
	vectordb_loader: VectorDBLoader,
	llm: LLM,
	app_config: TConfig,
	query: str,
	ctx_limit: int = 20,
	scope_type: ScopeType | None = None,
	scope_list: list[str] | None = None,
	template: str | None = None,
	end_separator: str = '',
):
	"""
	Raises
	------
	ValueError
		If the context length is too small to fit the query
	"""
	db = vectordb_loader.load()
	context_docs = get_context_docs(user_id, query, db, ctx_limit, scope_type, scope_list)
	if len(context_docs) == 0:
		raise ContextException('No documents retrieved, please index a few documents first')

	context_chunks = get_context_chunks(context_docs)
	logger.debug('context retrieved', extra={
		'len(context_docs)': len(context_docs),
		'len(context_chunks)': len(context_chunks),
	})

	output = llm.invoke(
		get_pruned_query(llm, app_config, query, template or _LLM_TEMPLATE, context_chunks),
		stop=[end_separator],
		userid=user_id,
	).strip()
	unique_sources: list[str] = list({source for d in context_docs if (source := d.metadata.get('source'))})

	return LLMOutput(output=output, sources=unique_sources)
