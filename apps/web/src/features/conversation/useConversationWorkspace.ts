import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import type { Account } from "../../api/auth";
import { createConversation, deleteConversation, getConversation, listConversations, renameConversation, streamRun, type Conversation, type RunEvent } from "../../api/conversations";
import type { CitationSelection } from "../citations/CitationPreviewPanel";
import { errorMessage } from "./presentation";

export function useConversationWorkspace(account: Account, active: boolean) {
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [stage, setStage] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [citationSelection, setCitationSelection] = useState<CitationSelection | null>(null);
  const [deleteConversationOpen, setDeleteConversationOpen] = useState(false);

  const conversationListKey = ["conversations", account.id] as const;
  const conversationDetailKey = ["conversation", account.id, selectedId] as const;
  const conversations = useQuery({ queryKey: conversationListKey, queryFn: listConversations, enabled: active });
  const detail = useQuery({ queryKey: conversationDetailKey, queryFn: () => getConversation(selectedId as string), enabled: active && selectedId !== null });

  useEffect(() => {
    setSelectedId(null);
    setEditing(false);
    setError(null);
    setCitationSelection(null);
  }, [account.id]);

  useEffect(() => {
    setCitationSelection(null);
  }, [selectedId]);

  useEffect(() => {
    if (!active) setCitationSelection(null);
  }, [active]);

  useEffect(() => {
    if (selectedId === null && conversations.data?.items[0]) setSelectedId(conversations.data.items[0].id);
  }, [conversations.data, selectedId]);

  const selected: Conversation | undefined = useMemo(() => conversations.data?.items.find((item) => item.id === selectedId), [conversations.data, selectedId]);
  const createMutation = useMutation({ mutationFn: createConversation, onSuccess: async (item) => { setError(null); await client.invalidateQueries({ queryKey: conversationListKey }); setSelectedId(item.id); }, onError: (cause) => setError(errorMessage(cause)) });
  const renameMutation = useMutation({ mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, title), onSuccess: async () => { setError(null); setEditing(false); await client.invalidateQueries({ queryKey: conversationListKey }); await detail.refetch(); }, onError: (cause) => setError(errorMessage(cause)) });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: async () => {
      setDeleteConversationOpen(false);
      setCitationSelection(null);
      setSelectedId(null);
      setError(null);
      await client.invalidateQueries({ queryKey: conversationListKey });
    },
    onError: (cause) => setError(errorMessage(cause)),
  });

  function saveTitle() {
    if (!selected) return;
    const currentTitle = detail.data?.title ?? selected.title;
    const normalizedTitle = titleDraft.trim();

    if (!normalizedTitle || normalizedTitle === currentTitle) {
      setTitleDraft(currentTitle);
      setError(null);
      setEditing(false);
      return;
    }

    setError(null);
    renameMutation.mutate({ id: selected.id, title: normalizedTitle });
  }

  async function send() {
    const content = draft.trim();
    if (!selectedId || !content || streaming) return;
    setError(null); setStreaming(true); setStage(null); setStreamText(""); setDraft("");
    let generateStarted = false;
    let accepted = false;
    let requestId: string | null = null;
    let runId: string | null = null;
    let terminal = false;
    try {
      await streamRun(selectedId, content, crypto.randomUUID(), (event: RunEvent) => {
        if (!event.request_id || !event.run_id || terminal) return;
        requestId ??= event.request_id;
        runId ??= event.run_id;
        accepted = true;
        if (event.request_id !== requestId || event.run_id !== runId) return;
        if (event.type === "stage.started") { generateStarted = event.stage === "GENERATE"; setStage(event.stage ?? null); }
        if (event.type === "answer.delta" && generateStarted && event.delta) setStreamText((value) => value + event.delta);
        if (event.type === "run.completed" || event.type === "run.failed") {
          terminal = true;
        }
      });
      await detail.refetch(); await client.invalidateQueries({ queryKey: conversationListKey });
    } catch (cause) {
      if (!accepted) setError(errorMessage(cause));
      await detail.refetch(); await client.invalidateQueries({ queryKey: conversationListKey });
    } finally { setStreaming(false); }
  }


  return {
    selectedId, setSelectedId, draft, setDraft, editing, setEditing, titleDraft, setTitleDraft,
    stage, streaming, streamText, error, setError, citationSelection, setCitationSelection,
    deleteConversationOpen, setDeleteConversationOpen, conversations, detail, selected,
    createMutation, renameMutation, deleteMutation, saveTitle, send,
  };
}

export type ConversationWorkspaceState = ReturnType<typeof useConversationWorkspace>;
