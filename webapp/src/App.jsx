import React, { useState, useEffect, useRef } from 'react';
import { 
  Database, 
  Send, 
  ChevronRight, 
  ChevronDown, 
  Table, 
  BarChart3, 
  Code, 
  RefreshCcw, 
  MessageSquare,
  Layout,
  Sun,
  Moon,
  PlusCircle,
  History,
  PieChart as PieIcon,
  Search,
  ArrowRight,
  Link as LinkIcon,
  ZoomIn,
  ZoomOut,
  Maximize2,
  Edit2,
  Trash2,
  Check,
  X,
  Square
} from 'lucide-react';
import { 
  BarChart, 
  Bar, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  ResponsiveContainer, 
  PieChart, 
  Pie, 
  Cell,
  LineChart,
  Line
} from 'recharts';
import ReactFlow, { 
  Background, 
  Controls, 
  Handle, 
  Position,
  useNodesState,
  useEdgesState,
  ReactFlowProvider,
  useReactFlow,
  Panel
} from 'reactflow';
import 'reactflow/dist/style.css';

const COLORS = ['#3b82f6', '#8b5cf6', '#10b981', '#f59e0b', '#ef4444'];

function App() {
  const [theme, setTheme] = useState(localStorage.getItem('theme') || 'dark');
  const [view, setView] = useState('chat'); // 'chat' | 'schema' | 'reporting'
  const [question, setQuestion] = useState('');
  const [messages, setMessages] = useState(JSON.parse(localStorage.getItem('current_chat')) || []);
  const [chatHistory, setChatHistory] = useState(JSON.parse(localStorage.getItem('chat_history')) || []);
  const [schema, setSchema] = useState({});
  const [relations, setRelations] = useState([]);
  const [schemaMode, setSchemaMode] = useState('grid'); // 'grid' | 'diagram'
  const [expandedTables, setExpandedTables] = useState({});
  const [editingChatId, setEditingChatId] = useState(null);
  const [currentChatId, setCurrentChatId] = useState(localStorage.getItem('current_chat_id') || null);
  const [chatToDelete, setChatToDelete] = useState(null);
  const [editTitle, setEditTitle] = useState('');
  const chatEndRef = useRef(null);
  const activeChatRef = useRef(currentChatId);
  const abortControllerRef = useRef(null);
  const [editingMessageIndex, setEditingMessageIndex] = useState(null);
  const [tempMessageText, setTempMessageText] = useState('');

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem('current_chat', JSON.stringify(messages));
  }, [messages]);

  useEffect(() => {
    localStorage.setItem('chat_history', JSON.stringify(chatHistory));
  }, [chatHistory]);

  useEffect(() => {
    if (currentChatId) {
      localStorage.setItem('current_chat_id', currentChatId);
    } else {
      localStorage.removeItem('current_chat_id');
    }
    activeChatRef.current = currentChatId;
  }, [currentChatId]);

  useEffect(() => {
    fetchSchema();
    fetchRelations();
  }, []);

  useEffect(() => {
    fetchSchema();
    fetchRelations();
  }, []);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const fetchSchema = async () => {
    try {
      const res = await fetch('http://localhost:8000/schema');
      const data = await res.json();
      setSchema(data);
    } catch (err) {
      console.error("Error fetching schema:", err);
    }
  };

  const fetchRelations = async () => {
    try {
      const res = await fetch('http://localhost:8000/relations');
      const data = await res.json();
      setRelations(data);
    } catch (err) {
      console.error("Error fetching relations:", err);
    }
  };

  const handleSend = async (overrideText = null, editIndex = null) => {
    const textToSend = overrideText || question;
    if (!textToSend.trim()) return;

    let requestId = currentChatId;
    let newMessages;

    if (editIndex !== null) {
      const baseMessages = messages.slice(0, editIndex);
      const userMsg = { role: 'user', text: textToSend };
      const loadingMsg = { role: 'bot', loading: true };
      newMessages = [...baseMessages, userMsg, loadingMsg];
    } else if (!currentChatId) {
      requestId = Date.now().toString();
      const userMsg = { role: 'user', text: textToSend };
      const loadingMsg = { role: 'bot', loading: true };
      newMessages = [userMsg, loadingMsg];
      
      const chatTitle = textToSend.length > 30 ? textToSend.substring(0, 30) + "..." : textToSend;
      const newChat = { id: requestId, title: chatTitle, messages: newMessages };
      setChatHistory(prev => [newChat, ...prev]);
      setCurrentChatId(requestId);
      activeChatRef.current = requestId;
    } else {
      const userMsg = { role: 'user', text: textToSend };
      const loadingMsg = { role: 'bot', loading: true };
      newMessages = [...messages, userMsg, loadingMsg];
      
      setChatHistory(prev => prev.map(c => c.id === currentChatId ? { ...c, messages: newMessages } : c));
    }

    setMessages(newMessages);
    if (!overrideText) setQuestion('');
    
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const res = await fetch('http://localhost:8000/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: textToSend }),
        signal: controller.signal
      });
      const data = await res.json();
      
      const botMsg = { 
        role: 'bot', 
        sql: data.sql,
        success: data.success,
        results: data.results,
        error: data.error,
        attempts: data.attempts,
        schema_tables: data.schema_tables
      };
      
      // Always update history
      setChatHistory(prev => prev.map(c => c.id === requestId ? { 
        ...c, 
        messages: [...c.messages.filter(m => !m.loading), botMsg] 
      } : c));

      // Only update current view if we are still on the same chat
      if (activeChatRef.current === requestId) {
        setMessages(prev => [...prev.filter(m => !m.loading), botMsg]);
      }

    } catch (err) {
      const errorMsg = { 
        role: 'bot', 
        error: "Impossible de contacter le serveur API." 
      };
      setChatHistory(prev => prev.map(c => c.id === requestId ? { 
        ...c, 
        messages: [...c.messages.filter(m => !m.loading), errorMsg] 
      } : c));
      
      if (activeChatRef.current === requestId) {
        setMessages(prev => [...prev.filter(m => !m.loading), errorMsg]);
      }
    } finally {
      abortControllerRef.current = null;
    }
  };

  const handleAbort = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
      
      const abortedMsg = { 
        role: 'bot', 
        error: "Génération interrompue par l'utilisateur." 
      };
      
      setMessages(prev => [...prev.filter(m => !m.loading), abortedMsg]);
      setChatHistory(prev => prev.map(c => c.id === activeChatRef.current ? { 
        ...c, 
        messages: [...c.messages.filter(m => !m.loading), abortedMsg] 
      } : c));
    }
  };

  const startEditMessage = (index, text) => {
    setEditingMessageIndex(index);
    setTempMessageText(text);
  };

  const handleEditSubmit = (index) => {
    if (!tempMessageText.trim()) return;
    
    // Truncate history from this message onwards
    const newMessages = messages.slice(0, index);
    const updatedUserMsg = { role: 'user', text: tempMessageText };
    
    setMessages([...newMessages, updatedUserMsg]);
    setEditingMessageIndex(null);
    
    // Trigger regeneration with the new text
    handleSend(tempMessageText, index);
  };

  const toggleTheme = () => setTheme(prev => prev === 'dark' ? 'light' : 'dark');

  const startNewChat = () => {
    setMessages([]);
    setCurrentChatId(null);
    setView('chat');
  };

  const loadChat = (chat) => {
    setMessages(chat.messages);
    setCurrentChatId(chat.id);
    setView('chat');
  };

  const deleteChat = (id, e) => {
    e.stopPropagation();
    setChatToDelete(id);
  };

  const confirmDelete = () => {
    if (chatToDelete) {
      setChatHistory(prev => prev.filter(c => c.id !== chatToDelete));
      if (currentChatId === chatToDelete) {
        startNewChat();
      }
      setChatToDelete(null);
    }
  };

  const startEditChat = (chat, e) => {
    e.stopPropagation();
    setEditingChatId(chat.id);
    setEditTitle(chat.title);
  };

  const saveChatTitle = (id, e) => {
    if (e) e.stopPropagation();
    if (!editTitle.trim()) {
      setEditingChatId(null);
      return;
    }
    setChatHistory(prev => prev.map(c => c.id === id ? { ...c, title: editTitle } : c));
    setEditingChatId(null);
  };

  const cancelEditChat = (e) => {
    e.stopPropagation();
    setEditingChatId(null);
  };

  const toggleTable = (tableName) => {
    setExpandedTables(prev => ({ ...prev, [tableName]: !prev[tableName] }));
  };

  return (
    <div className="app-container">
      {/* Sidebar - Enhanced */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <h2 style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
            <Code size={20} style={{color: 'var(--accent)'}} />
            <span style={{color: 'var(--accent)'}}>Txt2SQL</span>
            <span style={{color: 'white', marginLeft: '-4px'}}>Expert</span>
          </h2>
        </div>

        <div className="sidebar-content">
          <div className="nav-section">
            <button className="nav-btn" onClick={startNewChat} style={{width: '100%', border: 'none', background: 'var(--accent)', color: 'white', marginBottom: '12px'}}>
              <PlusCircle size={18} /> Nouveau Chat
            </button>
            
            <h3>Navigation</h3>
            <div className={`nav-btn ${view === 'chat' ? 'active' : ''}`} onClick={() => setView('chat')}>
              <MessageSquare size={18} /> Chat
            </div>
            <div className={`nav-btn ${view === 'schema' ? 'active' : ''}`} onClick={() => setView('schema')}>
              <Database size={18} /> Explorer le schéma
            </div>
            <div className={`nav-btn ${view === 'reporting' ? 'active' : ''}`} onClick={() => setView('reporting')}>
              <BarChart3 size={18} /> Reporting
            </div>
          </div>

          {chatHistory.length > 0 && (
            <div className="nav-section" style={{flex: 1, overflowY: 'auto', maxHeight: '400px'}}>
              <h3>Historique</h3>
              {chatHistory.map(chat => (
                <div key={chat.id} className="history-item-wrapper">
                  {editingChatId === chat.id ? (
                    <div style={{display: 'flex', width: '100%', alignItems: 'center'}}>
                      <input 
                        type="text" 
                        value={editTitle} 
                        onChange={(e) => setEditTitle(e.target.value)}
                        className="history-edit-input"
                        autoFocus
                        onKeyDown={(e) => e.key === 'Enter' && saveChatTitle(chat.id)}
                      />
                      <button className="action-btn" onClick={(e) => saveChatTitle(chat.id, e)}><Check size={14} className="text-accent" /></button>
                      <button className="action-btn" onClick={cancelEditChat}><X size={14} /></button>
                    </div>
                  ) : (
                    <>
                      <div className="history-item" onClick={() => loadChat(chat)} title={chat.title}>
                        <History size={14} style={{marginRight: '8px', flexShrink: 0}} />
                        <span style={{overflow: 'hidden', textOverflow: 'ellipsis'}}>{chat.title}</span>
                      </div>
                      <div className="history-item-actions">
                        <button className="action-btn" onClick={(e) => startEditChat(chat, e)} title="Renommer">
                          <Edit2 size={14} />
                        </button>
                        <button className="action-btn delete" onClick={(e) => deleteChat(chat.id, e)} title="Supprimer">
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="sidebar-footer">
          <span style={{fontSize: '0.8rem', color: 'var(--text-secondary)'}}>v1.5 • Qwen2.5-Coder</span>
          <button className="theme-toggle" onClick={toggleTheme}>
            {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </aside>

      {/* Confirmation Modal */}
      {chatToDelete && (
        <div className="modal-overlay" onClick={() => setChatToDelete(null)}>
          <div className="modal-content" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <Trash2 size={24} />
              <h3 style={{margin: 0, fontSize: '1.2rem'}}>Supprimer la conversation ?</h3>
            </div>
            <p style={{color: 'var(--text-secondary)', fontSize: '0.95rem', lineHeight: 1.6}}>
              Cette action est irréversible. Toutes les requêtes et les résultats associés seront perdus.
            </p>
            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={() => setChatToDelete(null)}>Annuler</button>
              <button className="btn btn-danger" onClick={confirmDelete}>Supprimer définitivement</button>
            </div>
          </div>
        </div>
      )}

      <main className="main-content">
        <header className="header">
          <div style={{display: 'flex', alignItems: 'center', gap: '12px'}}>
            {view === 'chat' && <MessageSquare size={20} />}
            {view === 'schema' && <Database size={20} />}
            {view === 'reporting' && <BarChart3 size={20} />}
            <h1 style={{fontSize: '1.1rem'}}>
              {view === 'chat' && "Chat Assistant"}
              {view === 'schema' && "Explorateur de Schéma"}
              {view === 'reporting' && "Tableau de Bord Reporting"}
            </h1>
          </div>
          <div className="text-secondary" style={{fontSize: '0.8rem'}}>
            Autocorrection Active
          </div>
        </header>

        <div className="chat-area">
          {view === 'chat' && (
            <>
              {messages.length === 0 && (
                <div style={{textAlign: 'center', marginTop: '100px', color: 'var(--text-secondary)'}}>
                  <MessageSquare size={48} style={{opacity: 0.2, marginBottom: '16px'}} />
                  <p>Posez une question sur vos ventes, produits ou clients.</p>
                  <p style={{fontSize: '0.8rem'}}>Ex: "Quels sont les 5 produits les plus vendus ?"</p>
                </div>
              )}
              {messages.map((msg, idx) => (
                msg.role === 'user' ? (
                  <div key={idx} className="message-wrapper user-message-wrapper" style={{display: 'flex', flexDirection: 'column', alignItems: 'flex-end', marginBottom: '20px'}}>
                    <div className={`message ${msg.role}`} style={{position: 'relative', marginBottom: '4px'}}>
                      {editingMessageIndex === idx ? (
                        <div className="message-editor">
                          <textarea 
                            value={tempMessageText}
                            onChange={(e) => setTempMessageText(e.target.value)}
                            className="editor-textarea"
                            autoFocus
                          />
                          <div className="editor-footer">
                            <button className="btn-cancel" onClick={() => setEditingMessageIndex(null)}>Annuler</button>
                            <button className="btn-save" onClick={() => handleEditSubmit(idx)}>Régénérer la réponse</button>
                          </div>
                        </div>
                      ) : (
                        <div style={{wordBreak: 'break-word'}}>{msg.text}</div>
                      )}
                    </div>
                    {editingMessageIndex !== idx && (
                      <div className="message-actions" style={{paddingRight: '10px', opacity: 0, transition: 'opacity 0.2s'}}>
                        <button 
                          className="action-btn edit-msg-btn-v3" 
                          onClick={() => startEditMessage(idx, msg.text)}
                          title="Modifier la question"
                          style={{background: 'transparent', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', display: 'flex'}}
                        >
                          <Edit2 size={14} />
                        </button>
                      </div>
                    )}
                  </div>
                ) : (
                  <div key={idx} className={`message ${msg.role}`}>
                    {msg.loading ? (
                      <div style={{display: 'flex', alignItems: 'center', gap: '12px'}}>
                        <RefreshCcw size={18} className="animate-spin text-accent" />
                        <span style={{color: 'var(--text-secondary)', fontSize: '0.9rem'}}>Génération et exécution en cours...</span>
                      </div>
                    ) : (
                      <BotResponse msg={msg} />
                    )}
                  </div>
                )
              ))}
              <div ref={chatEndRef} />
            </>
          )}

          {view === 'schema' && (
            <div className="schema-view">
              <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px'}}>
                <p style={{color: 'var(--text-secondary)', margin: 0}}>Structure complète de la base de données ERP.</p>
                <div className="tabs" style={{margin: 0}}>
                  <div className={`tab ${schemaMode === 'grid' ? 'active' : ''}`} onClick={() => setSchemaMode('grid')}>
                    <Table size={14} /> Grille
                  </div>
                  <div className={`tab ${schemaMode === 'diagram' ? 'active' : ''}`} onClick={() => setSchemaMode('diagram')}>
                    <LinkIcon size={14} /> Associations
                  </div>
                </div>
              </div>

              {schemaMode === 'grid' ? (
                <div style={{display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: '12px'}}>
                  {Object.entries(schema).map(([tableName, columns]) => (
                    <div key={tableName} className="message bot" style={{padding: '12px 16px', margin: 0, borderRadius: '8px'}}>
                      <div style={{display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px', color: 'var(--accent)', fontWeight: '600', fontSize: '0.9rem'}}>
                        <Database size={16} /> {tableName}
                      </div>
                      <div className="column-list" style={{marginLeft: 0}}>
                        {columns.map(col => (
                          <div key={col.name} className="column-item" style={{borderBottom: '1px solid var(--border)', padding: '4px 0', fontSize: '0.8rem'}}>
                            <span style={{fontWeight: col.is_pk ? 'bold' : 'normal'}}>{col.name} {col.is_pk ? '(PK)' : ''}</span>
                            <span style={{color: 'var(--text-secondary)', fontSize: '0.7rem'}}>{col.type}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div style={{height: '700px', width: '100%', border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden'}}>
                  <SchemaRelationsView schema={schema} relations={relations} theme={theme} />
                </div>
              )}
            </div>
          )}

          {view === 'reporting' && (
            <ReportingView />
          )}
        </div>

        {view === 'chat' && (
          <div className="input-container">
            <div className="input-box">
              <input 
                type="text" 
                placeholder="Posez votre question..." 
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSend()}
              />
              {messages.some(m => m.loading) ? (
                <button className="send-btn" onClick={handleAbort} style={{background: '#ef4444'}}>
                  <Square size={18} fill="white" />
                </button>
              ) : (
                <button className="send-btn" onClick={handleSend} disabled={!question.trim()}>
                  <Send size={18} />
                </button>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

function BotResponse({ msg }) {
  const [activeTab, setActiveTab] = useState('table');
  const [chartType, setChartType] = useState('bar');
  
  // Logic to determine if we can show a chart
  const hasData = msg.results && msg.results.columns && msg.results.rows && msg.results.rows.length > 0;
  const numericCols = hasData ? msg.results.columns.filter((col, i) => {
    return i > 0 && typeof msg.results.rows[0][i] === 'number';
  }) : [];
  
  const [selectedCol, setSelectedCol] = useState(numericCols.length > 0 ? numericCols[0] : (hasData ? msg.results.columns[1] : null));

  if (msg.error && !msg.success) {
    return (
      <div style={{color: '#ef4444'}}>
        <p><strong>Erreur :</strong> {msg.error}</p>
        {msg.sql && (
          <div style={{marginTop: '12px'}}>
            <p style={{fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '4px'}}>SQL généré :</p>
            <pre style={{background: '#000', padding: '10px', borderRadius: '4px', fontSize: '0.8rem', overflowX: 'auto'}}>{msg.sql}</pre>
          </div>
        )}
      </div>
    );
  }

  const canShowChart = hasData && msg.results.columns.length >= 2;
  const labelCol = hasData ? msg.results.columns[0] : null;
  const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899'];

  const chartData = hasData ? msg.results.rows.map(row => {
    const obj = {};
    msg.results.columns.forEach((col, i) => obj[col] = row[i]);
    return obj;
  }) : [];

  return (
    <div className="results-container">
      <div className="tabs">
        <div className={`tab ${activeTab === 'table' ? 'active' : ''}`} onClick={() => setActiveTab('table')}>
          <Table size={14} /> Données
        </div>
        {canShowChart && (
          <div className={`tab ${activeTab === 'chart' ? 'active' : ''}`} onClick={() => setActiveTab('chart')}>
            <BarChart3 size={14} /> Graphique
          </div>
        )}
        <div className={`tab ${activeTab === 'sql' ? 'active' : ''}`} onClick={() => setActiveTab('sql')}>
          <Code size={14} /> SQL
        </div>
      </div>

      <div className="tab-content">
        {activeTab === 'table' && (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  {msg.results.columns.map(col => <th key={col}>{col}</th>)}
                </tr>
              </thead>
              <tbody>
                {msg.results.rows.map((row, idx) => (
                  <tr key={idx}>
                    {row.map((cell, i) => <td key={i}>{cell}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {activeTab === 'chart' && (
          <div style={{ display: 'flex', gap: '24px', marginTop: '20px', alignItems: 'flex-start' }}>
            <div style={{ height: '350px', flex: 1, background: 'var(--bg-secondary)', padding: '20px', borderRadius: 'var(--radius)', border: '1px solid var(--border)' }}>
              <ResponsiveContainer>
                {chartType === 'bar' ? (
                  <BarChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                    <XAxis dataKey={labelCol} stroke="#888" fontSize={11} tickLine={false} axisLine={false} />
                    <YAxis stroke="#888" fontSize={11} tickLine={false} axisLine={false} />
                    <Tooltip 
                      contentStyle={{ backgroundColor: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '8px' }}
                      itemStyle={{ color: 'var(--text-primary)' }}
                    />
                    <Bar dataKey={selectedCol} fill="var(--accent)" radius={[4, 4, 0, 0]} />
                  </BarChart>
                ) : chartType === 'line' ? (
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                    <XAxis dataKey={labelCol} stroke="#888" fontSize={11} tickLine={false} axisLine={false} />
                    <YAxis stroke="#888" fontSize={11} tickLine={false} axisLine={false} />
                    <Tooltip 
                      contentStyle={{ backgroundColor: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '8px' }}
                      itemStyle={{ color: 'var(--text-primary)' }}
                    />
                    <Line type="monotone" dataKey={selectedCol} stroke="var(--accent)" strokeWidth={3} dot={{ r: 4, fill: 'var(--accent)' }} activeDot={{ r: 6 }} />
                  </LineChart>
                ) : (
                  <PieChart>
                    <Pie
                      data={chartData}
                      cx="50%"
                      cy="50%"
                      innerRadius={60}
                      outerRadius={100}
                      paddingAngle={5}
                      dataKey={selectedCol}
                      nameKey={labelCol}
                      label
                    >
                      {chartData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip 
                      contentStyle={{ backgroundColor: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: '8px' }}
                    />
                  </PieChart>
                )}
              </ResponsiveContainer>
            </div>

            {/* Sidebar Controls */}
            <div style={{ width: '200px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
              {/* Chart Type Selector */}
              <div style={{
                background: 'var(--bg-tertiary)',
                padding: '12px',
                borderRadius: 'var(--radius)',
                border: '1px solid var(--border)'
              }}>
                <p style={{fontSize: '0.7rem', fontWeight: '700', textTransform: 'uppercase', color: 'var(--text-secondary)', marginBottom: '8px'}}>Type de vue</p>
                <div style={{display: 'flex', flexDirection: 'column', gap: '4px'}}>
                  <div 
                    onClick={() => setChartType('bar')}
                    style={{
                      padding: '8px 12px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer',
                      background: chartType === 'bar' ? 'var(--accent)' : 'transparent',
                      color: chartType === 'bar' ? 'white' : 'var(--text-secondary)'
                    }}
                  >
                    Histogramme
                  </div>
                  <div 
                    onClick={() => setChartType('line')}
                    style={{
                      padding: '8px 12px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer',
                      background: chartType === 'line' ? 'var(--accent)' : 'transparent',
                      color: chartType === 'line' ? 'white' : 'var(--text-secondary)'
                    }}
                  >
                    Courbe
                  </div>
                  <div 
                    onClick={() => setChartType('pie')}
                    style={{
                      padding: '8px 12px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer',
                      background: chartType === 'pie' ? 'var(--accent)' : 'transparent',
                      color: chartType === 'pie' ? 'white' : 'var(--text-secondary)'
                    }}
                  >
                    Camembert
                  </div>
                </div>
              </div>

              {/* Column Selector */}
              {numericCols.length > 1 && (
                <div style={{
                  background: 'var(--bg-tertiary)',
                  padding: '12px',
                  borderRadius: 'var(--radius)',
                  border: '1px solid var(--border)'
                }}>
                  <p style={{fontSize: '0.7rem', fontWeight: '700', textTransform: 'uppercase', color: 'var(--text-secondary)', marginBottom: '8px'}}>Donnée affichée</p>
                  <div style={{display: 'flex', flexDirection: 'column', gap: '4px'}}>
                    {numericCols.map(col => (
                      <div 
                        key={col}
                        onClick={() => setSelectedCol(col)}
                        style={{
                          padding: '8px 12px', fontSize: '0.85rem', borderRadius: '6px', cursor: 'pointer',
                          background: selectedCol === col ? 'var(--accent)' : 'transparent',
                          color: selectedCol === col ? 'white' : 'var(--text-secondary)',
                          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'
                        }}
                      >
                        {col}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {activeTab === 'sql' && (
          <pre style={{
            background: 'var(--bg-tertiary)', 
            padding: '16px', 
            borderRadius: '8px', 
            overflowX: 'auto',
            fontSize: '0.85rem',
            color: 'var(--accent)',
            border: '1px solid var(--border)'
          }}>
            {msg.sql}
          </pre>
        )}
      </div>
      <div style={{marginTop: '12px', fontSize: '0.75rem', color: 'var(--text-secondary)'}}>
        Résolu en {msg.attempts} tentative(s)
      </div>
    </div>
  );
}

// Custom Node for Database Tables
const TableNode = ({ data }) => {
  return (
    <div style={{
      background: 'var(--bg-secondary)',
      border: '1px solid var(--border)',
      borderRadius: '12px',
      minWidth: '220px',
      boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
      overflow: 'hidden',
      cursor: 'default'
    }}>
      <div style={{
        background: 'var(--bg-tertiary)',
        padding: '10px 16px',
        borderBottom: '1px solid var(--border)',
        fontWeight: '700',
        fontSize: '1rem',
        color: 'var(--accent)',
        textAlign: 'center'
      }}>
        {data.label}
      </div>
      <div style={{ padding: '8px 0' }}>
        {data.columns.map((col, i) => (
          <div key={i} style={{ 
            padding: '6px 16px', 
            display: 'flex', 
            justifyContent: 'space-between', 
            alignItems: 'center',
            fontSize: '0.85rem',
            position: 'relative'
          }}>
            <Handle 
              type="target" 
              position={Position.Left} 
              id={`${data.label}-${col.name}-target`} 
              style={{ visibility: 'hidden' }} 
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ color: 'var(--text-primary)', fontWeight: col.is_pk ? '600' : '400' }}>{col.name}</span>
              {col.is_pk && <span style={{fontSize: '0.7rem', background: 'var(--bg-tertiary)', padding: '2px 4px', borderRadius: '4px', opacity: 0.8}}>PK</span>}
              {data.isFk(col.name) && <span style={{fontSize: '0.7rem', background: 'var(--bg-tertiary)', padding: '2px 4px', borderRadius: '4px', opacity: 0.8}}>FK</span>}
            </div>
            <span style={{ color: 'var(--text-secondary)', fontSize: '0.75rem' }}>{col.type.split('(')[0]}</span>
            <Handle 
              type="source" 
              position={Position.Right} 
              id={`${data.label}-${col.name}-source`} 
              style={{ visibility: 'hidden' }} 
            />
          </div>
        ))}
      </div>
    </div>
  );
};

const nodeTypes = { tableNode: TableNode };

function FlowContent({ schema, relations, theme }) {
  const { zoomIn, zoomOut, fitView } = useReactFlow();

  const initialNodes = Object.entries(schema).map(([tableName, columns], index) => ({
    id: tableName,
    type: 'tableNode',
    data: { 
      label: tableName, 
      columns: columns,
      isFk: (colName) => relations.some(r => r.from === tableName && r.from_col === colName)
    },
    position: { x: (index % 5) * 400, y: Math.floor(index / 5) * 350 },
  }));

  const initialEdges = relations.map((rel, index) => ({
    id: `e-${index}`,
    source: rel.to,
    target: rel.from,
    sourceHandle: `${rel.to}-${rel.to_col}-source`,
    targetHandle: `${rel.from}-${rel.from_col}-target`,
    animated: false,
    style: { stroke: 'var(--accent)', strokeWidth: 2 },
    type: 'smoothstep'
  }));

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  return (
    <div style={{ height: '100%', width: '100%', background: 'var(--bg-primary)' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        panOnDrag={true}
        panOnScroll={true}
        selectionOnDrag={false}
      >
        <Background color={theme === 'dark' ? '#333' : '#ccc'} gap={20} variant="dots" />
        <Panel position="top-right" style={{ display: 'flex', gap: '8px', padding: '10px' }}>
          <button className="theme-toggle" onClick={() => zoomOut()} title="Zoom Out">
            <ZoomOut size={16} />
          </button>
          <button className="theme-toggle" onClick={() => fitView()} title="Reset">
            <Maximize2 size={16} />
          </button>
          <button className="theme-toggle" onClick={() => zoomIn()} title="Zoom In">
            <ZoomIn size={16} />
          </button>
        </Panel>
      </ReactFlow>
    </div>
  );
}

function SchemaRelationsView(props) {
  return (
    <ReactFlowProvider>
      <FlowContent {...props} />
    </ReactFlowProvider>
  );
}

function ReportingView() {
  const [reports, setReports] = useState(() => {
    const saved = localStorage.getItem('dashboard_reports');
    if (saved) return JSON.parse(saved);
    return [
      { id: 1, title: "Top 5 Produits par Ventes", question: "Quels sont les 5 produits les plus vendus ?", data: null, type: 'bar' },
      { id: 2, title: "Ventes par Boutique", question: "Quel est le chiffre d'affaires total par boutique ?", data: null, type: 'pie' },
      { id: 3, title: "Évolution des Commandes", question: "Combien de commandes par mois sur les 6 derniers mois ?", data: null, type: 'line' },
    ];
  });
  
  const [newTitle, setNewTitle] = useState('');
  const [newQuery, setNewQuery] = useState('');
  const [newType, setNewType] = useState('bar');
  const [loading, setLoading] = useState({});
  const [isAdding, setIsAdding] = useState(false);

  useEffect(() => {
    localStorage.setItem('dashboard_reports', JSON.stringify(reports));
  }, [reports]);

  const runReport = async (id, question) => {
    setLoading(prev => ({ ...prev, [id]: true }));
    try {
      const res = await fetch('http://localhost:8000/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question })
      });
      const data = await res.json();
      setReports(prev => prev.map(r => r.id === id ? { ...r, data: data } : r));
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(prev => ({ ...prev, [id]: false }));
    }
  };

  const handleAddWidget = async () => {
    if (!newQuery.trim()) return;
    
    setIsAdding(true);
    const newId = Date.now();
    const finalTitle = newTitle.trim() || newQuery.trim();
    const newReport = {
      id: newId,
      title: finalTitle,
      question: newQuery,
      data: null,
      type: newType
    };
    
    setReports(prev => [newReport, ...prev]);
    setNewQuery('');
    setNewTitle('');
    
    // Auto-run the new report
    await runReport(newId, newQuery);
    setIsAdding(false);
  };

  const deleteReport = (id) => {
    setReports(prev => prev.filter(r => r.id !== id));
  };

  return (
    <div className="reporting-container">
      {/* Add Widget Header */}
      <div className="message bot" style={{padding: '24px', marginBottom: '30px', border: '1px solid var(--accent)', background: 'rgba(59, 130, 246, 0.05)', borderRadius: 'var(--radius-lg)', maxWidth: 'none'}}>
        <div style={{display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px'}}>
          <PlusCircle size={20} className="text-accent" />
          <h3 style={{margin: 0, fontSize: '1.1rem', fontWeight: '600'}}>Ajouter un indicateur au tableau de bord</h3>
        </div>
        
        <div style={{display: 'flex', flexDirection: 'column', gap: '12px'}}>
          <input 
            type="text" 
            placeholder="Titre du widget (ex: Ventes Mensuelles)" 
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            style={{width: '100%', padding: '10px 14px', fontSize: '0.95rem'}}
          />
          
          <div style={{display: 'flex', gap: '10px', flexWrap: 'wrap'}}>
            <input 
              type="text" 
              placeholder="Posez votre question (ex: Quels sont les revenus par mois en 2025 ?)" 
              value={newQuery}
              onChange={(e) => setNewQuery(e.target.value)}
              style={{flex: 1, minWidth: '300px', padding: '10px 14px'}}
              onKeyDown={(e) => e.key === 'Enter' && handleAddWidget()}
            />
            <select 
              value={newType} 
              onChange={(e) => setNewType(e.target.value)}
              style={{width: '160px', background: 'var(--bg-tertiary)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', color: 'var(--text-primary)', padding: '0 12px', height: '40px'}}
            >
              <option value="bar">Histogramme</option>
              <option value="line">Courbe</option>
              <option value="pie">Camembert</option>
            </select>
            <button 
              className="nav-btn active" 
              onClick={handleAddWidget}
              disabled={isAdding || !newQuery.trim()}
              style={{margin: 0, height: '40px', padding: '0 20px'}}
            >
              {isAdding ? <RefreshCcw size={16} className="animate-spin" /> : <PlusCircle size={16} />} 
              <span style={{marginLeft: '8px'}}>Ajouter</span>
            </button>
          </div>
        </div>
      </div>

      {/* Grid */}
      <div className="reporting-grid" style={{display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(450px, 1fr))', gap: '24px'}}>
        {reports.map(report => (
          <div key={report.id} className="message bot" style={{padding: '24px', margin: 0, position: 'relative', display: 'flex', flexDirection: 'column'}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '20px'}}>
              <div>
                <h3 style={{fontSize: '1rem', fontWeight: '600', marginBottom: '4px'}}>{report.title}</h3>
                <p style={{fontSize: '0.75rem', color: 'var(--text-secondary)', margin: 0}}>{report.question}</p>
              </div>
              <div style={{display: 'flex', gap: '8px'}}>
                <button 
                  className="action-btn" 
                  onClick={() => runReport(report.id, report.question)}
                  disabled={loading[report.id]}
                  title="Actualiser"
                >
                  {loading[report.id] ? <RefreshCcw size={14} className="animate-spin" /> : <RefreshCcw size={14} />}
                </button>
                <button 
                  className="action-btn delete" 
                  onClick={() => deleteReport(report.id)}
                  title="Supprimer"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>

            <div style={{flex: 1, minHeight: '250px'}}>
              {!report.data && !loading[report.id] && (
                <div style={{height: '250px', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-primary)', borderRadius: '8px', color: 'var(--text-secondary)'}}>
                  Cliquez sur actualiser pour générer
                </div>
              )}

              {loading[report.id] && (
                <div style={{height: '250px', display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
                  <div style={{textAlign: 'center'}}>
                    <RefreshCcw size={24} className="animate-spin" style={{color: 'var(--accent)', marginBottom: '12px'}} />
                    <p style={{fontSize: '0.8rem', color: 'var(--text-secondary)'}}>Génération SQL en cours...</p>
                  </div>
                </div>
              )}

              {report.data && report.data.success && (
                <div style={{height: '250px'}}>
                  <ResponsiveContainer width="100%" height="100%">
                    {report.type === 'bar' ? (
                      <BarChart data={report.data.results.rows.map(row => {
                        const obj = {};
                        report.data.results.columns.forEach((col, i) => obj[col] = row[i]);
                        return obj;
                      })}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                        <XAxis dataKey={report.data.results.columns[0]} stroke="#888" fontSize={10} tickLine={false} axisLine={false} />
                        <YAxis stroke="#888" fontSize={10} tickLine={false} axisLine={false} />
                        <Tooltip 
                          contentStyle={{background: 'var(--bg-secondary)', border: '1px solid var(--border)', color: 'var(--text-primary)', borderRadius: '8px'}} 
                        />
                        <Bar dataKey={report.data.results.columns[1]} fill="#3b82f6" radius={[4, 4, 0, 0]} />
                      </BarChart>
                    ) : report.type === 'pie' ? (
                      <PieChart>
                        <Pie
                          data={report.data.results.rows.map(row => ({ name: row[0], value: row[1] }))}
                          cx="50%"
                          cy="50%"
                          outerRadius={80}
                          innerRadius={50}
                          paddingAngle={5}
                          fill="#8884d8"
                          dataKey="value"
                          label
                        >
                          {report.data.results.rows.map((entry, index) => (
                            <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                          ))}
                        </Pie>
                        <Tooltip />
                      </PieChart>
                    ) : (
                      <LineChart data={report.data.results.rows.map(row => {
                        const obj = {};
                        report.data.results.columns.forEach((col, i) => obj[col] = row[i]);
                        return obj;
                      })}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                        <XAxis dataKey={report.data.results.columns[0]} stroke="#888" fontSize={10} tickLine={false} axisLine={false} />
                        <YAxis stroke="#888" fontSize={10} tickLine={false} axisLine={false} />
                        <Tooltip 
                          contentStyle={{background: 'var(--bg-secondary)', border: '1px solid var(--border)', color: 'var(--text-primary)', borderRadius: '8px'}} 
                        />
                        <Line type="monotone" dataKey={report.data.results.columns[1]} stroke="#10b981" strokeWidth={3} dot={{ r: 4, fill: '#10b981' }} activeDot={{ r: 6 }} />
                      </LineChart>
                    )}
                  </ResponsiveContainer>
                </div>
              )}

              {report.data && !report.data.success && (
                <div style={{height: '250px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'rgba(239, 68, 68, 0.05)', borderRadius: '8px', color: '#ef4444', padding: '20px', textAlign: 'center'}}>
                  <X size={24} style={{marginBottom: '12px'}} />
                  <p style={{fontSize: '0.9rem', fontWeight: '600', margin: '0 0 8px 0'}}>Erreur de génération</p>
                  <p style={{fontSize: '0.75rem', opacity: 0.8}}>{report.data.error}</p>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default App;
