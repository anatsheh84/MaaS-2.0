import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  Page,
  PageSection,
  Title,
  Split,
  SplitItem,
  Card,
  CardTitle,
  CardBody,
  TextInput,
  Button,
  FormGroup,
  Progress,
  ProgressVariant,
  Label,
  Select,
  SelectOption,
  MenuToggle,
  MenuToggleElement,
  TextArea,
  Flex,
  FlexItem,
  Divider,
  EmptyState,
  EmptyStateBody,
  Spinner,
  Alert,
  AlertActionCloseButton,
} from '@patternfly/react-core';
import {
  PaperPlaneIcon,
  TrashIcon,
  PlusCircleIcon,
  BookOpenIcon,
  ArrowLeftIcon,
} from '@patternfly/react-icons';

const API_BASE = window.location.hostname === 'localhost' ? '' : '/api';

interface ModelOption { value: string; label: string }
interface ChatMessage { role: 'user' | 'assistant'; content: string }
interface IngestJob { status: string; progress?: number; filename?: string; error?: string }
interface DocEntry { doc_id: string; filename: string; ingest_status: string }
interface NotebookEntry {
  notebook_id: string;
  name: string;
  file_counts?: { completed?: number; total?: number };
  status?: string;
  created_at?: number;
}

export const App: React.FC = () => {
  // ── Notebook state ──
  const [notebookName, setNotebookName] = useState('');
  const [notebookId, setNotebookId] = useState<string | null>(null);
  const [activeNotebookName, setActiveNotebookName] = useState('');
  const [creating, setCreating] = useState(false);
  const [existingNotebooks, setExistingNotebooks] = useState<NotebookEntry[]>([]);
  const [loadingNotebooks, setLoadingNotebooks] = useState(true);
  const [apiError, setApiError] = useState<string | null>(null);

  // ── Documents state ──
  const [documents, setDocuments] = useState<DocEntry[]>([]);
  const [uploading, setUploading] = useState(false);
  const [ingestJobs, setIngestJobs] = useState<Record<string, IngestJob>>({});
  const ingestPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Chat state ──
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [query, setQuery] = useState('');
  const [models, setModels] = useState<ModelOption[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [modelSelectOpen, setModelSelectOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // ── API error handler ──
  const handleApiError = useCallback((resp: Response, resetNb = false) => {
    if (resp.status === 404 && resetNb) {
      setApiError('Notebook no longer exists. Please create or select another.');
      setNotebookId(null); setActiveNotebookName('');
      setDocuments([]); setMessages([]);
    } else if (resp.status === 429) {
      setApiError('Too many requests — please wait a moment.');
    } else if (resp.status >= 500) {
      setApiError(`Server error (${resp.status}) — the API may be restarting.`);
    } else {
      setApiError(`Request failed (HTTP ${resp.status}).`);
    }
  }, []);

  // ── Load notebooks list ──
  const loadNotebooks = useCallback(async () => {
    setLoadingNotebooks(true);
    try {
      const resp = await fetch(`${API_BASE}/notebooks`);
      if (resp.ok) {
        const data = await resp.json();
        setExistingNotebooks(data.notebooks || []);
      }
    } catch { /* ignore */ }
    finally { setLoadingNotebooks(false); }
  }, []);

  useEffect(() => { loadNotebooks(); }, [loadNotebooks]);

  // ── Load models ──
  useEffect(() => {
    fetch(`${API_BASE}/models`)
      .then((r) => r.json())
      .then((data) => {
        const list: ModelOption[] = data.models || [];
        setModels(list);
        if (list.length > 0) setSelectedModel(list[0].value);
      })
      .catch(() => {
        const fallback = [{ value: 'qwen3-4b-instruct', label: 'Qwen3 4B Instruct' }];
        setModels(fallback);
        setSelectedModel(fallback[0].value);
      });
  }, []);

  // ── Ingest polling ──
  const startIngestPoll = useCallback((nbId: string) => {
    if (ingestPollRef.current) clearInterval(ingestPollRef.current);
    ingestPollRef.current = setInterval(async () => {
      try {
        const resp = await fetch(`${API_BASE}/notebooks/${nbId}/ingest-status`);
        if (!resp.ok) { if (resp.status === 404) handleApiError(resp, true); return; }
        const data = await resp.json();
        setIngestJobs(data.jobs || {});
        const allDone = Object.values(data.jobs as Record<string, IngestJob>).every(
          (j) => j.status === 'completed' || j.status === 'failed',
        );
        if (allDone && Object.keys(data.jobs).length > 0) {
          if (ingestPollRef.current) clearInterval(ingestPollRef.current);
          ingestPollRef.current = null;
        }
      } catch { /* ignore */ }
    }, 3000);
  }, [handleApiError]);

  useEffect(() => () => {
    if (ingestPollRef.current) clearInterval(ingestPollRef.current);
  }, []);

  // ── Notebook actions ──
  const refreshDocuments = async (nbId: string) => {
    const resp = await fetch(`${API_BASE}/notebooks/${nbId}/documents`);
    if (resp.ok) { setDocuments((await resp.json()).documents || []); }
    else if (resp.status === 404) handleApiError(resp, true);
  };

  const selectNotebook = async (nb: NotebookEntry) => {
    setApiError(null);
    setNotebookId(nb.notebook_id);
    setActiveNotebookName(nb.name);
    setMessages([]); setIngestJobs({});
    await refreshDocuments(nb.notebook_id);
  };

  const createNotebook = async () => {
    if (!notebookName.trim()) return;
    setCreating(true); setApiError(null);
    try {
      const resp = await fetch(`${API_BASE}/notebooks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: notebookName.trim() }),
      });
      if (!resp.ok) { handleApiError(resp); return; }
      const data = await resp.json();
      setNotebookId(data.notebook_id);
      setActiveNotebookName(notebookName.trim());
      setNotebookName('');
      setMessages([]); setDocuments([]); setIngestJobs({});
      await loadNotebooks();
    } finally { setCreating(false); }
  };

  const goBackToList = () => {
    setNotebookId(null); setActiveNotebookName('');
    setMessages([]); setDocuments([]); setIngestJobs({});
    if (ingestPollRef.current) { clearInterval(ingestPollRef.current); ingestPollRef.current = null; }
    loadNotebooks();
  };

  // ── File upload ──
  const uploadFiles = async (files: FileList | File[]) => {
    const fileArray = Array.from(files);
    if (!notebookId || fileArray.length === 0) return;
    setUploading(true); setApiError(null);
    try {
      for (const file of fileArray) {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(`${API_BASE}/notebooks/${notebookId}/documents`, {
          method: 'POST', body: formData,
        });
        if (!resp.ok) handleApiError(resp, resp.status === 404);
      }
      await refreshDocuments(notebookId);
      startIngestPoll(notebookId);
    } finally { setUploading(false); }
  };

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) uploadFiles(e.target.files);
    e.target.value = '';
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
  };

  // ── SSE chat ──
  const sendMessage = async () => {
    if (!query.trim() || !notebookId || streaming) return;
    const userQuery = query.trim();
    setQuery(''); setStreaming(true); setApiError(null);

    let assistantIdx = 0;
    setMessages((prev) => {
      assistantIdx = prev.length + 1;
      return [...prev, { role: 'user', content: userQuery },
                       { role: 'assistant', content: '' }];
    });

    try {
      const response = await fetch(`${API_BASE}/notebooks/${notebookId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ query: userQuery, model: selectedModel }),
      });

      if (!response.ok || !response.body) {
        handleApiError(response, response.status === 404);
        setMessages((prev) => {
          const updated = [...prev];
          updated[assistantIdx] = { role: 'assistant', content: `Error: HTTP ${response.status}` };
          return updated;
        });
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (payload === '[DONE]') continue;
          try {
            const parsed = JSON.parse(payload);
            let token = '';
            if (parsed?.choices?.[0]?.delta?.content) {
              token = parsed.choices[0].delta.content;
            } else if (typeof parsed?.text === 'string') {
              token = parsed.text;
            }
            if (token) {
              setMessages((prev) => {
                const updated = [...prev];
                const msg = updated[assistantIdx];
                updated[assistantIdx] = { ...msg, content: msg.content + token };
                return updated;
              });
            }
          } catch { /* skip malformed */ }
        }
      }
    } finally { setStreaming(false); }
  };

  const handleChatKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  };

  const clearChat = () => setMessages([]);

  const ingestVariant = (status: string): ProgressVariant | undefined => {
    if (status === 'completed') return ProgressVariant.success;
    if (status === 'failed') return ProgressVariant.danger;
    return undefined;
  };

  // ── Render ──
  return (
    <Page>
      <PageSection variant="default">
        <Title headingLevel="h1" size="2xl">
          <BookOpenIcon /> NotebookLM
        </Title>
      </PageSection>

      {apiError && (
        <PageSection style={{ paddingTop: 0, paddingBottom: 0 }}>
          <Alert
            variant="danger"
            title={apiError}
            actionClose={<AlertActionCloseButton onClose={() => setApiError(null)} />}
            style={{ marginBottom: 8 }}
          />
        </PageSection>
      )}

      <PageSection>
        <Split hasGutter>
          {/* ═══════════ Panel 1: Notebook Setup ═══════════ */}
          <SplitItem style={{ flex: 1, minWidth: 360 }}>
            <Card>
              <CardTitle>Notebooks</CardTitle>
              <CardBody>
                {!notebookId ? (
                  <>
                    {/* Create new */}
                    <FormGroup label="Create new notebook" fieldId="nb-name">
                      <Flex>
                        <FlexItem grow={{ default: 'grow' }}>
                          <TextInput
                            id="nb-name"
                            value={notebookName}
                            onChange={(_e, val) => setNotebookName(val)}
                            placeholder="My research notebook"
                            onKeyDown={(e) => e.key === 'Enter' && createNotebook()}
                          />
                        </FlexItem>
                        <FlexItem>
                          <Button
                            icon={<PlusCircleIcon />}
                            onClick={createNotebook}
                            isLoading={creating}
                            isDisabled={!notebookName.trim() || creating}
                          >
                            Create
                          </Button>
                        </FlexItem>
                      </Flex>
                    </FormGroup>

                    {/* Existing notebooks list */}
                    <Divider style={{ margin: '16px 0' }} />
                    <FormGroup label="Your notebooks" fieldId="nb-list">
                      {loadingNotebooks ? (
                        <Flex justifyContent={{ default: 'justifyContentCenter' }}>
                          <Spinner size="md" />
                        </Flex>
                      ) : existingNotebooks.length === 0 ? (
                        <div style={{ fontSize: 13, color: '#6a6e73', textAlign: 'center', padding: '12px 0' }}>
                          No notebooks yet. Create one above.
                        </div>
                      ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                          {existingNotebooks.map((nb) => (
                            <div
                              key={nb.notebook_id}
                              onClick={() => selectNotebook(nb)}
                              style={{
                                padding: '10px 12px',
                                borderRadius: 6,
                                border: '1px solid #d2d2d2',
                                cursor: 'pointer',
                                background: '#fafafa',
                                transition: 'background 0.15s',
                              }}
                              onMouseEnter={(e) => (e.currentTarget.style.background = '#e7f1fa')}
                              onMouseLeave={(e) => (e.currentTarget.style.background = '#fafafa')}
                            >
                              <div style={{ fontWeight: 600, fontSize: 14 }}>
                                <BookOpenIcon style={{ marginRight: 6 }} />
                                {nb.name}
                              </div>
                              <div style={{ fontSize: 12, color: '#6a6e73', marginTop: 2 }}>
                                {nb.file_counts?.total || 0} document{(nb.file_counts?.total || 0) !== 1 ? 's' : ''}
                                {nb.status === 'completed' && (
                                  <Label color="green" style={{ marginLeft: 8 }} isCompact>ready</Label>
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </FormGroup>
                  </>
                ) : (
                  <>
                    {/* Active notebook view */}
                    <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 12 }}>
                      <FlexItem>
                        <Button variant="link" icon={<ArrowLeftIcon />} onClick={goBackToList} style={{ paddingLeft: 0 }}>
                          All notebooks
                        </Button>
                      </FlexItem>
                    </Flex>
                    <FormGroup label="Active notebook" fieldId="nb-active">
                      <Label color="blue" style={{ fontSize: 14 }}>
                        <BookOpenIcon style={{ marginRight: 4 }} /> {activeNotebookName}
                      </Label>
                    </FormGroup>

                    <Divider style={{ margin: '16px 0' }} />

                    <FormGroup label="Upload documents" fieldId="nb-upload">
                      <div
                        onDrop={handleDrop}
                        onDragOver={(e) => e.preventDefault()}
                        style={{
                          border: '2px dashed var(--pf-v5-global--BorderColor--100, #d2d2d2)',
                          borderRadius: 8,
                          padding: '20px 16px',
                          textAlign: 'center',
                          cursor: 'pointer',
                          background: uploading ? '#f5f5f5' : 'transparent',
                        }}
                        onClick={() => document.getElementById('file-input')?.click()}
                      >
                        <input
                          id="file-input"
                          type="file"
                          accept=".pdf,.txt,.docx"
                          multiple
                          style={{ display: 'none' }}
                          onChange={handleFileInput}
                        />
                        {uploading ? (
                          <><Spinner size="md" /> <span style={{ marginLeft: 8, fontSize: 13 }}>Uploading...</span></>
                        ) : (
                          <span style={{ fontSize: 13, color: '#6a6e73' }}>
                            Click to select or drag files here<br/>
                            <small>PDF, TXT, DOCX accepted</small>
                          </span>
                        )}
                      </div>
                      {documents.length > 0 && (
                        <div style={{ marginTop: 8, fontSize: 13, color: '#6a6e73' }}>
                          {documents.map((doc) => (
                            <div key={doc.doc_id}>📄 {doc.filename}</div>
                          ))}
                        </div>
                      )}
                    </FormGroup>

                    {/* Ingest status */}
                    {Object.keys(ingestJobs).length > 0 && (
                      <>
                        <Divider style={{ margin: '16px 0' }} />
                        <FormGroup label="Ingest status" fieldId="nb-ingest">
                          {Object.entries(ingestJobs).map(([docId, job]) => (
                            <div key={docId} style={{ marginBottom: 12 }}>
                              <div style={{ marginBottom: 4, fontSize: 13 }}>
                                {job.filename || docId} &mdash;{' '}
                                <Label color={job.status === 'completed' ? 'green' : job.status === 'failed' ? 'red' : 'blue'}>
                                  {job.status}
                                </Label>
                              </div>
                              <Progress value={job.progress || 0} title="" size="sm" variant={ingestVariant(job.status)} />
                              {job.error && (
                                <div style={{ color: '#c9190b', fontSize: 12, marginTop: 4 }}>{job.error}</div>
                              )}
                            </div>
                          ))}
                        </FormGroup>
                      </>
                    )}
                  </>
                )}
              </CardBody>
            </Card>
          </SplitItem>

          {/* ═══════════ Panel 2: Chat Interface ═══════════ */}
          <SplitItem style={{ flex: 2, minWidth: 480 }}>
            <Card style={{ display: 'flex', flexDirection: 'column', height: '80vh' }}>
              <CardTitle>
                <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }}>
                  <FlexItem>Chat</FlexItem>
                  <FlexItem>
                    <Flex spaceItems={{ default: 'spaceItemsSm' }}>
                      <FlexItem>
                        <Select
                          isOpen={modelSelectOpen}
                          selected={selectedModel}
                          onSelect={(_e, val) => { setSelectedModel(val as string); setModelSelectOpen(false); }}
                          onOpenChange={setModelSelectOpen}
                          toggle={(toggleRef: React.Ref<MenuToggleElement>) => (
                            <MenuToggle
                              ref={toggleRef}
                              onClick={() => setModelSelectOpen(!modelSelectOpen)}
                              isExpanded={modelSelectOpen}
                              style={{ minWidth: 200 }}
                            >
                              {models.find((m) => m.value === selectedModel)?.label ?? 'Select model'}
                            </MenuToggle>
                          )}
                        >
                          {models.map((m) => (
                            <SelectOption key={m.value} value={m.value}>{m.label}</SelectOption>
                          ))}
                        </Select>
                      </FlexItem>
                      <FlexItem>
                        <Button
                          variant="plain"
                          icon={<TrashIcon />}
                          onClick={clearChat}
                          aria-label="Clear conversation"
                          isDisabled={messages.length === 0}
                        />
                      </FlexItem>
                    </Flex>
                  </FlexItem>
                </Flex>
              </CardTitle>

              <CardBody style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
                {messages.length === 0 && !notebookId ? (
                  <EmptyState>
                    <EmptyStateBody>
                      Create or select a notebook to start chatting.
                    </EmptyStateBody>
                  </EmptyState>
                ) : messages.length === 0 ? (
                  <EmptyState>
                    <EmptyStateBody>
                      Upload a document, wait for ingest, then ask a question.
                    </EmptyStateBody>
                  </EmptyState>
                ) : (
                  messages.map((msg, i) => (
                    <div
                      key={i}
                      style={{
                        display: 'flex',
                        justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
                        marginBottom: 12,
                      }}
                    >
                      <div
                        style={{
                          maxWidth: '75%',
                          padding: '10px 14px',
                          borderRadius: 8,
                          background: msg.role === 'user' ? '#0066cc' : '#f0f0f0',
                          color: msg.role === 'user' ? '#fff' : '#151515',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                          fontSize: 14,
                          lineHeight: 1.5,
                        }}
                      >
                        {msg.content || (streaming && i === messages.length - 1 ? (
                          <Spinner size="sm" />
                        ) : null)}
                      </div>
                    </div>
                  ))
                )}
                <div ref={chatEndRef} />
              </CardBody>

              <div style={{ padding: '12px 16px', borderTop: '1px solid #d2d2d2' }}>
                <Flex>
                  <FlexItem grow={{ default: 'grow' }}>
                    <TextArea
                      value={query}
                      onChange={(_e, val) => setQuery(val)}
                      onKeyDown={handleChatKeyDown}
                      placeholder={
                        notebookId
                          ? 'Ask a question about your documents...'
                          : 'Select a notebook first'
                      }
                      isDisabled={!notebookId || streaming}
                      rows={1}
                      autoResize
                      aria-label="Chat input"
                    />
                  </FlexItem>
                  <FlexItem alignSelf={{ default: 'alignSelfFlexEnd' }}>
                    <Button
                      icon={<PaperPlaneIcon />}
                      onClick={sendMessage}
                      isDisabled={!notebookId || !query.trim() || streaming}
                      isLoading={streaming}
                    >
                      Send
                    </Button>
                  </FlexItem>
                </Flex>
              </div>
            </Card>
          </SplitItem>
        </Split>
      </PageSection>
    </Page>
  );
};
