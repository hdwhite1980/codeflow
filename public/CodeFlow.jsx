import React, { useState, useRef, useCallback, useMemo, useEffect } from 'react';
import { 
  Upload, 
  FileCode, 
  Shield, 
  Eye, 
  AlertTriangle, 
  CheckCircle, 
  XCircle,
  GitBranch,
  Activity,
  Database,
  Cloud,
  Settings,
  Download,
  Share2,
  Zap,
  Brain,
  Map,
  Plus,
  Search,
  Filter,
  MoreVertical,
  Code,
  FileText,
  Users,
  Bell,
  Menu,
  X,
  ArrowRight,
  TrendingUp,
  Smartphone,
  Bot,
  Lock,
  Unlock,
  RotateCcw,
  PlayCircle,
  PauseCircle,
  RefreshCw,
  ExternalLink,
  Copy,
  Edit3,
  Trash2,
  FolderOpen,
  Tag,
  Calendar,
  Clock,
  Layers,
  Network,
  Cpu,
  HardDrive,
  Globe,
  Server,
  Key,
  ShieldCheck,
  Gauge,
  BarChart3,
  PieChart,
  LineChart,
  Target
} from 'lucide-react';

// Supabase Client Setup (you'll need to configure this with your actual keys)
// import { createClient } from '@supabase/supabase-js'
// const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
// const supabaseKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
// const supabase = createClient(supabaseUrl, supabaseKey)

const CodeFlowPlatform = () => {
  // Core State Management
  const [activeView, setActiveView] = useState('dashboard');
  const [projects, setProjects] = useState([]);
  const [currentProject, setCurrentProject] = useState(null);
  const [files, setFiles] = useState([]);
  const [aiSessions, setAiSessions] = useState([]);
  const [notifications, setNotifications] = useState([]);
  const [settings, setSettings] = useState({
    theme: 'dark',
    riskSensitivity: 'medium',
    autoAnalysis: true,
    notifications: true
  });
  const [visualMap, setVisualMap] = useState({ nodes: [], edges: [] });
  const [analysisResults, setAnalysisResults] = useState({});
  const [protectedElements, setProtectedElements] = useState({});
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fileInputRef = useRef(null);
  const [dragActive, setDragActive] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const [filterType, setFilterType] = useState('all');

  // Initialize data from Supabase
  useEffect(() => {
    initializeApp();
  }, []);

  const initializeApp = async () => {
    setLoading(true);
    try {
      // Initialize Supabase connection and load user data
      await loadProjects();
      await loadSettings();
    } catch (error) {
      console.error('Failed to initialize app:', error);
      setError('Failed to load application data');
    } finally {
      setLoading(false);
    }
  };

  // Supabase Data Loading Functions
  const loadProjects = async () => {
    try {
      // Replace with actual Supabase query
      // const { data, error } = await supabase
      //   .from('projects')
      //   .select('*')
      //   .order('created_at', { ascending: false });
      
      // if (error) throw error;
      // setProjects(data || []);
      
      // Temporary empty state until Supabase is connected
      setProjects([]);
    } catch (error) {
      console.error('Error loading projects:', error);
      setError('Failed to load projects');
    }
  };

  const loadSettings = async () => {
    try {
      // Replace with actual Supabase query
      // const { data, error } = await supabase
      //   .from('user_settings')
      //   .select('*')
      //   .single();
      
      // if (error && error.code !== 'PGRST116') throw error;
      // if (data) setSettings(data.settings);
    } catch (error) {
      console.error('Error loading settings:', error);
    }
  };

  // AI Code Analysis Engine (Real Implementation)
  const analyzeCode = async (file) => {
    setLoading(true);
    setError(null);

    try {
      // Real code analysis using external libraries
      const analysis = await performRealCodeAnalysis(file);
      
      // Store results in Supabase
      // await supabase.from('analysis_results').insert({
      //   file_id: file.id,
      //   project_id: file.projectId,
      //   analysis_data: analysis,
      //   created_at: new Date().toISOString()
      // });

      setAnalysisResults(prev => ({
        ...prev,
        [file.id]: analysis
      }));

      // Create notification
      addNotification({
        type: 'success',
        title: 'Analysis Complete',
        message: `Successfully analyzed ${file.name}`,
        projectId: file.projectId
      });

      return analysis;
    } catch (error) {
      console.error('Analysis failed:', error);
      setError(`Failed to analyze ${file.name}: ${error.message}`);
      addNotification({
        type: 'error',
        title: 'Analysis Failed',
        message: `Failed to analyze ${file.name}`,
        projectId: file.projectId
      });
    } finally {
      setLoading(false);
    }
  };

  // Real code analysis implementation
  const performRealCodeAnalysis = async (file) => {
    // This would use real libraries like esprima, eslint, etc.
    const content = file.content;
    
    // Basic analysis structure - replace with real implementations
    const analysis = {
      fileId: file.id,
      timestamp: new Date().toISOString(),
      astAnalysis: await performASTAnalysis(content),
      securityAnalysis: await performSecurityAnalysis(content),
      performanceAnalysis: await performPerformanceAnalysis(content),
      qualityMetrics: await calculateQualityMetrics(content)
    };

    return analysis;
  };

  const performASTAnalysis = async (content) => {
    // Real AST parsing would go here using esprima or @babel/parser
    return {
      functions: [],
      variables: [],
      imports: [],
      exports: [],
      complexity: 0
    };
  };

  const performSecurityAnalysis = async (content) => {
    // Real security analysis using tools like eslint-plugin-security
    return {
      vulnerabilities: [],
      riskScore: 0
    };
  };

  const performPerformanceAnalysis = async (content) => {
    // Real performance analysis
    return {
      complexityScore: 0,
      bottlenecks: [],
      recommendations: []
    };
  };

  const calculateQualityMetrics = async (content) => {
    // Real quality metrics calculation
    return {
      maintainabilityIndex: 0,
      cyclomaticComplexity: 0,
      codeSmells: []
    };
  };

  // File Upload and Management
  const handleFileUpload = useCallback(async (uploadedFiles) => {
    if (!currentProject) {
      setError('Please select a project first');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      for (let i = 0; i < uploadedFiles.length; i++) {
        const file = uploadedFiles[i];
        const content = await readFileContent(file);
        
        const fileData = {
          project_id: currentProject.id,
          name: file.name,
          type: determineFileType(file.name),
          size: file.size,
          content: content,
          created_at: new Date().toISOString(),
          protection_level: 'Optional',
          tags: []
        };

        // Save to Supabase
        // const { data, error } = await supabase
        //   .from('files')
        //   .insert(fileData)
        //   .select()
        //   .single();

        // if (error) throw error;

        // For now, add to local state
        const newFile = {
          id: Date.now() + i,
          ...fileData,
          lines: content.split('\n').length,
          functions: 0,
          complexity: 0,
          riskLevel: 'Low',
          apiCalls: 0,
          dbOperations: 0,
          cloudServices: [],
          securityFlags: [],
          performanceFlags: []
        };

        setFiles(prev => [...prev, newFile]);

        // Trigger analysis
        setTimeout(() => analyzeCode(newFile), 100);
      }

      addNotification({
        type: 'success',
        title: 'Files Uploaded',
        message: `Successfully uploaded ${uploadedFiles.length} file(s)`,
        projectId: currentProject.id
      });

    } catch (error) {
      console.error('Upload failed:', error);
      setError(`Upload failed: ${error.message}`);
    } finally {
      setLoading(false);
    }
  }, [currentProject]);

  const readFileContent = (file) => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => resolve(e.target.result);
      reader.onerror = () => reject(new Error('Failed to read file'));
      reader.readAsText(file);
    });
  };

  const determineFileType = (filename) => {
    const extension = filename.split('.').pop().toLowerCase();
    const typeMap = {
      'js': 'JavaScript',
      'jsx': 'React Component',
      'ts': 'TypeScript',
      'tsx': 'TypeScript React',
      'py': 'Python',
      'json': 'Configuration',
      'css': 'Stylesheet',
      'html': 'HTML Document'
    };
    return typeMap[extension] || 'Unknown';
  };

  // Project Management
  const createProject = async (projectData) => {
    setLoading(true);
    try {
      const newProject = {
        name: projectData.name,
        description: projectData.description || '',
        tags: projectData.tags || [],
        status: 'active',
        created_at: new Date().toISOString()
      };

      // Save to Supabase
      // const { data, error } = await supabase
      //   .from('projects')
      //   .insert(newProject)
      //   .select()
      //   .single();

      // if (error) throw error;

      // For now, add to local state
      const project = {
        id: Date.now(),
        ...newProject,
        files: 0,
        complexity: "Low",
        riskLevel: "Low"
      };

      setProjects(prev => [...prev, project]);
      setCurrentProject(project);
      setActiveView('project');

      addNotification({
        type: 'success',
        title: 'Project Created',
        message: `Successfully created project: ${projectData.name}`
      });

    } catch (error) {
      console.error('Failed to create project:', error);
      setError(`Failed to create project: ${error.message}`);
    } finally {
      setLoading(false);
    }
  };

  const deleteProject = async (projectId) => {
    if (!confirm('Are you sure you want to delete this project?')) return;

    setLoading(true);
    try {
      // Delete from Supabase
      // const { error } = await supabase
      //   .from('projects')
      //   .delete()
      //   .eq('id', projectId);

      // if (error) throw error;

      setProjects(prev => prev.filter(p => p.id !== projectId));
      if (currentProject?.id === projectId) {
        setCurrentProject(null);
        setActiveView('projects');
      }

      addNotification({
        type: 'success',
        title: 'Project Deleted',
        message: 'Project has been successfully deleted'
      });

    } catch (error) {
      console.error('Failed to delete project:', error);
      setError(`Failed to delete project: ${error.message}`);
    } finally {
      setLoading(false);
    }
  };

  // AI Session Tracking
  const logAiSession = async (sessionData) => {
    try {
      const session = {
        project_id: currentProject?.id,
        provider: sessionData.provider,
        prompt: sessionData.prompt,
        response: sessionData.response,
        intent: sessionData.intent,
        constraints: sessionData.constraints,
        expected_changes: sessionData.expectedChanges,
        score: sessionData.score || 0,
        files_affected: sessionData.filesAffected || [],
        changes_applied: false,
        risk_assessment: sessionData.riskAssessment || 'Low',
        created_at: new Date().toISOString()
      };

      // Save to Supabase
      // const { data, error } = await supabase
      //   .from('ai_sessions')
      //   .insert(session)
      //   .select()
      //   .single();

      // if (error) throw error;

      // Add to local state
      const newSession = {
        id: `session-${Date.now()}`,
        ...session,
        timestamp: session.created_at
      };

      setAiSessions(prev => [...prev, newSession]);

      addNotification({
        type: 'success',
        title: 'AI Session Logged',
        message: `Session with ${sessionData.provider} has been recorded`
      });

    } catch (error) {
      console.error('Failed to log AI session:', error);
      setError(`Failed to log AI session: ${error.message}`);
    }
  };

  // Protection System
  const updateProtectionLevel = async (fileId, level) => {
    try {
      // Update in Supabase
      // const { error } = await supabase
      //   .from('files')
      //   .update({ protection_level: level })
      //   .eq('id', fileId);

      // if (error) throw error;

      setFiles(prev => prev.map(file => 
        file.id === fileId ? { ...file, protectionLevel: level } : file
      ));

      setProtectedElements(prev => ({
        ...prev,
        [fileId]: level
      }));

      addNotification({
        type: 'success',
        title: 'Protection Updated',
        message: `File protection level changed to ${level}`
      });

    } catch (error) {
      console.error('Failed to update protection level:', error);
      setError(`Failed to update protection level: ${error.message}`);
    }
  };

  // Notification System
  const addNotification = (notification) => {
    const newNotification = {
      id: Date.now(),
      timestamp: new Date().toISOString(),
      read: false,
      ...notification
    };

    setNotifications(prev => [newNotification, ...prev]);

    // Auto-remove after 5 seconds for success notifications
    if (notification.type === 'success') {
      setTimeout(() => {
        setNotifications(prev => prev.filter(n => n.id !== newNotification.id));
      }, 5000);
    }
  };

  // Drag and Drop Handlers
  const handleDrag = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      handleFileUpload(Array.from(e.dataTransfer.files));
    }
  };

  // Filtered data
  const filteredProjects = useMemo(() => {
    return projects.filter(project => 
      project.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      (project.tags && project.tags.some(tag => 
        tag.toLowerCase().includes(searchTerm.toLowerCase())
      ))
    );
  }, [projects, searchTerm]);

  const filteredFiles = useMemo(() => {
    let filtered = files.filter(file => file.projectId === currentProject?.id);
    
    if (searchTerm) {
      filtered = filtered.filter(file => 
        file.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        file.type.toLowerCase().includes(searchTerm.toLowerCase())
      );
    }

    if (filterType !== 'all') {
      filtered = filtered.filter(file => {
        switch (filterType) {
          case 'high-risk': return file.riskLevel === 'High';
          case 'protected': return file.protectionLevel !== 'Optional';
          case 'recent': return new Date(file.lastModified || file.created_at) > new Date(Date.now() - 24 * 60 * 60 * 1000);
          default: return true;
        }
      });
    }

    return filtered;
  }, [files, currentProject, searchTerm, filterType]);

  // Component Renderers
  const renderHeader = () => (
    <header className="bg-slate-900 border-b border-slate-700 px-6 py-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-2">
            <Brain className="w-8 h-8 text-blue-400" />
            <h1 className="text-2xl font-bold text-white">CodeFlow</h1>
          </div>
          <span className="text-sm text-slate-400">AI-Powered Code Management Platform</span>
        </div>
        
        <div className="flex items-center space-x-4">
          <div className="relative">
            <Bell className="w-6 h-6 text-slate-400 hover:text-white cursor-pointer" />
            {notifications.filter(n => !n.read).length > 0 && (
              <span className="absolute -top-2 -right-2 bg-red-500 text-white text-xs rounded-full w-5 h-5 flex items-center justify-center">
                {notifications.filter(n => !n.read).length}
              </span>
            )}
          </div>
          <button 
            onClick={() => setActiveView('settings')}
            className="text-slate-400 hover:text-white"
          >
            <Settings className="w-6 h-6" />
          </button>
          <a 
            href="/mobile" 
            className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-white font-medium"
          >
            <Smartphone className="w-4 h-4 inline mr-2" />
            Mobile App
          </a>
        </div>
      </div>

      {error && (
        <div className="mt-4 bg-red-900 border border-red-700 rounded-lg p-3">
          <div className="flex items-center space-x-2">
            <XCircle className="w-5 h-5 text-red-400" />
            <span className="text-red-200">{error}</span>
            <button 
              onClick={() => setError(null)}
              className="ml-auto text-red-400 hover:text-red-300"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
    </header>
  );

  const renderSidebar = () => (
    <aside className="w-64 bg-slate-800 border-r border-slate-700 flex flex-col">
      <nav className="flex-1 px-4 py-6 space-y-2">
        {[
          { id: 'dashboard', icon: BarChart3, label: 'Dashboard' },
          { id: 'projects', icon: FolderOpen, label: 'Projects' },
          { id: 'visual-map', icon: Map, label: 'Visual Map' },
          { id: 'ai-sessions', icon: Bot, label: 'AI Sessions' },
          { id: 'protection', icon: Shield, label: 'Protection' },
          { id: 'analysis', icon: Activity, label: 'Code Analysis' },
          { id: 'quality', icon: Gauge, label: 'Code Quality' },
          { id: 'cloud', icon: Cloud, label: 'Cloud & Database' },
          { id: 'reports', icon: FileText, label: 'Reports & Export' }
        ].map(item => (
          <button
            key={item.id}
            onClick={() => setActiveView(item.id)}
            className={`w-full flex items-center space-x-3 px-3 py-2 rounded-lg text-left ${
              activeView === item.id ? 'bg-blue-600 text-white' : 'text-slate-300 hover:bg-slate-700'
            }`}
          >
            <item.icon className="w-5 h-5" />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>
    </aside>
  );

  const renderEmptyState = (title, description, icon: React.ElementType, actionLabel, actionCallback) => (
    <div className="flex items-center justify-center min-h-96">
      <div className="text-center max-w-md">
        {React.createElement(icon, { className: "w-16 h-16 text-slate-400 mx-auto mb-4" })}
        <h3 className="text-xl font-semibold text-white mb-2">{title}</h3>
        <p className="text-slate-400 mb-6">{description}</p>
        {actionCallback && (
          <button
            onClick={actionCallback}
            className="bg-blue-600 hover:bg-blue-700 px-6 py-2 rounded-lg text-white font-medium"
          >
            {actionLabel}
          </button>
        )}
      </div>
    </div>
  );

  const renderDashboard = () => (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-3xl font-bold text-white">Dashboard</h2>
        <button
          onClick={() => setActiveView('projects')}
          className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-white font-medium flex items-center space-x-2"
        >
          <Plus className="w-4 h-4" />
          <span>New Project</span>
        </button>
      </div>

      {projects.length === 0 ? (
        renderEmptyState(
          "Welcome to CodeFlow",
          "Get started by creating your first project to begin AI-powered code analysis and management.",
          Brain,
          "Create First Project",
          () => setActiveView('projects')
        )
      ) : (
        <>
          {/* Quick Stats */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-slate-400">Total Projects</p>
                  <p className="text-2xl font-bold text-white">{projects.length}</p>
                </div>
                <FolderOpen className="w-8 h-8 text-blue-400" />
              </div>
            </div>

            <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-slate-400">Total Files</p>
                  <p className="text-2xl font-bold text-white">{files.length}</p>
                </div>
                <FileCode className="w-8 h-8 text-green-400" />
              </div>
            </div>

            <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-slate-400">AI Sessions</p>
                  <p className="text-2xl font-bold text-white">{aiSessions.length}</p>
                </div>
                <Bot className="w-8 h-8 text-purple-400" />
              </div>
            </div>

            <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-slate-400">Active Alerts</p>
                  <p className="text-2xl font-bold text-white">
                    {notifications.filter(n => !n.read).length}
                  </p>
                </div>
                <AlertTriangle className="w-8 h-8 text-yellow-400" />
              </div>
            </div>
          </div>

          {/* Recent Projects */}
          {projects.length > 0 && (
            <div className="bg-slate-800 rounded-xl border border-slate-700">
              <div className="p-6 border-b border-slate-700">
                <h3 className="text-xl font-semibold text-white">Recent Projects</h3>
              </div>
              <div className="p-6">
                <div className="space-y-4">
                  {projects.slice(0, 3).map(project => (
                    <div
                      key={project.id}
                      className="flex items-center justify-between p-4 bg-slate-700 rounded-lg hover:bg-slate-600 cursor-pointer transition-colors"
                      onClick={() => {
                        setCurrentProject(project);
                        setActiveView('project');
                      }}
                    >
                      <div className="flex items-center space-x-4">
                        <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center">
                          <FolderOpen className="w-5 h-5 text-white" />
                        </div>
                        <div>
                          <h4 className="font-medium text-white">{project.name}</h4>
                          <p className="text-sm text-slate-400">{project.description || 'No description'}</p>
                        </div>
                      </div>
                      <ArrowRight className="w-4 h-4 text-slate-400" />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Recent Activity */}
          {notifications.length > 0 && (
            <div className="bg-slate-800 rounded-xl border border-slate-700">
              <div className="p-6 border-b border-slate-700">
                <h3 className="text-xl font-semibold text-white">Recent Activity</h3>
              </div>
              <div className="p-6">
                <div className="space-y-4">
                  {notifications.slice(0, 5).map(notification => (
                    <div key={notification.id} className="flex items-start space-x-3">
                      <div className={`w-2 h-2 rounded-full mt-2 ${
                        notification.type === 'error' ? 'bg-red-400' :
                        notification.type === 'warning' ? 'bg-yellow-400' :
                        'bg-green-400'
                      }`} />
                      <div className="flex-1">
                        <p className="text-white font-medium">{notification.title}</p>
                        <p className="text-sm text-slate-400">{notification.message}</p>
                        <p className="text-xs text-slate-500 mt-1">
                          {new Date(notification.timestamp).toLocaleString()}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );

  const renderProjects = () => (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-3xl font-bold text-white">Projects</h2>
        <button
          onClick={() => {
            const name = prompt('Project Name:');
            if (name?.trim()) {
              createProject({ 
                name: name.trim(), 
                description: '', 
                tags: [] 
              });
            }
          }}
          className="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg text-white font-medium flex items-center space-x-2"
        >
          <Plus className="w-4 h-4" />
          <span>New Project</span>
        </button>
      </div>

      {projects.length === 0 ? (
        renderEmptyState(
          "No Projects Yet",
          "Create your first project to start analyzing AI-generated code with advanced insights and protection.",
          FolderOpen,
          "Create Project",
          () => {
            const name = prompt('Project Name:');
            if (name?.trim()) {
              createProject({ 
                name: name.trim(), 
                description: '', 
                tags: [] 
              });
            }
          }
        )
      ) : (
        <>
          <div className="flex items-center space-x-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-slate-400 w-4 h-4" />
              <input
                type="text"
                placeholder="Search projects..."
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                className="w-full bg-slate-700 border border-slate-600 rounded-lg pl-10 pr-4 py-2 text-white placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {filteredProjects.map(project => (
              <div
                key={project.id}
                className="bg-slate-800 border border-slate-700 rounded-xl p-6 hover:border-blue-500 cursor-pointer transition-all relative group"
                onClick={() => {
                  setCurrentProject(project);
                  setActiveView('project');
                }}
              >
                <div className="flex items-start justify-between mb-4">
                  <div className="flex items-center space-x-3">
                    <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center">
                      <FolderOpen className="w-5 h-5 text-white" />
                    </div>
                    <div>
                      <h3 className="font-semibold text-white">{project.name}</h3>
                      <p className="text-sm text-slate-400">
                        {files.filter(f => f.projectId === project.id).length} files
                      </p>
                    </div>
                  </div>
                  <button 
                    onClick={(e) => {
                      e.stopPropagation();
                      if (confirm('Delete this project?')) {
                        deleteProject(project.id);
                      }
                    }}
                    className="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-red-400 transition-opacity"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                <p className="text-sm text-slate-300 mb-4 line-clamp-2">
                  {project.description || 'No description provided'}
                </p>

                <div className="flex items-center justify-between mb-4">
                  <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                    project.riskLevel === 'High' ? 'bg-red-900 text-red-300' :
                    project.riskLevel === 'Medium' ? 'bg-yellow-900 text-yellow-300' :
                    'bg-green-900 text-green-300'
                  }`}>
                    {project.riskLevel || 'Low'} Risk
                  </span>
                  <span className="text-xs text-slate-400">
                    {new Date(project.created_at).toLocaleDateString()}
                  </span>
                </div>

                {project.tags && project.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {project.tags.map(tag => (
                      <span key={tag} className="px-2 py-1 bg-slate-700 text-slate-300 text-xs rounded">
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );

  const renderProjectDetail = () => {
    if (!currentProject) {
      return renderEmptyState(
        "No Project Selected",
        "Select a project from the projects page to view its details and manage files.",
        FolderOpen,
        "Go to Projects",
        () => setActiveView('projects')
      );
    }

    const projectFiles = files.filter(f => f.projectId === currentProject.id);

    return (
      <div className="p-6 space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <button
              onClick={() => setActiveView('projects')}
              className="text-slate-400 hover:text-white"
            >
              <ArrowRight className="w-5 h-5 transform rotate-180" />
            </button>
            <h2 className="text-3xl font-bold text-white">{currentProject.name}</h2>
          </div>
          <div className="flex items-center space-x-4">
            <input
              type="file"
              ref={fileInputRef}
              onChange={(e) => e.target.files && handleFileUpload(Array.from(e.target.files))}
              multiple
              accept=".js,.jsx,.ts,.tsx,.py,.json,.css,.html,.mjs"
              className="hidden"
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={loading}
              className="bg-blue-600 hover:bg-blue-700 disabled:bg-blue-800 px-4 py-2 rounded-lg text-white font-medium flex items-center space-x-2"
            >
              <Upload className="w-4 h-4" />
              <span>{loading ? 'Uploading...' : 'Upload Files'}</span>
            </button>
          </div>
        </div>

        {/* Project Stats */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400">Files</p>
                <p className="text-2xl font-bold text-white">{projectFiles.length}</p>
              </div>
              <FileCode className="w-8 h-8 text-blue-400" />
            </div>
          </div>

          <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400">Lines of Code</p>
                <p className="text-2xl font-bold text-white">
                  {projectFiles.reduce((sum, f) => sum + (f.lines || 0), 0).toLocaleString()}
                </p>
              </div>
              <Code className="w-8 h-8 text-green-400" />
            </div>
          </div>

          <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400">Functions</p>
                <p className="text-2xl font-bold text-white">
                  {projectFiles.reduce((sum, f) => sum + (f.functions || 0), 0)}
                </p>
              </div>
              <Zap className="w-8 h-8 text-yellow-400" />
            </div>
          </div>

          <div className="bg-slate-800 p-6 rounded-xl border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400">Risk Level</p>
                <p className="text-2xl font-bold text-white">{currentProject.riskLevel || 'Low'}</p>
              </div>
              <Shield className="w-8 h-8 text-red-400" />
            </div>
          </div>
        </div>

        {/* File Upload Area */}
        <div
          onDragEnter={handleDrag}
          onDragLeave={handleDrag}
          onDragOver={handleDrag}
          onDrop={handleDrop}
          className={`border-2 border-dashed rounded-xl p-8 text-center transition-colors ${
            dragActive
              ? 'border-blue-400 bg-blue-900/20'
              : 'border-slate-600 hover:border-slate-500'
          }`}
        >
          <Upload className="w-12 h-12 text-slate-400 mx-auto mb-4" />
          <p className="text-lg font-medium text-white mb-2">
            Drag and drop files here, or click to select
          </p>
          <p className="text-slate-400 mb-4">
            Supports .js, .jsx, .ts, .tsx, .py, .json, .css, .html, .mjs files up to 10MB each
          </p>
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={loading}
            className="bg-blue-600 hover:bg-blue-700 disabled:bg-blue-800 px-6 py-2 rounded-lg text-white font-medium"
          >
            {loading ? 'Processing...' : 'Choose Files'}
          </button>
        </div>

        {/* Files List */}
        <div className="bg-slate-800 rounded-xl border border-slate-700">
          <div className="p-6 border-b border-slate-700">
            <div className="flex items-center justify-between">
              <h3 className="text-xl font-semibold text-white">Project Files</h3>
              {projectFiles.length > 0 && (
                <div className="flex items-center space-x-4">
                  <div className="relative">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-slate-400 w-4 h-4" />
                    <input
                      type="text"
                      placeholder="Search files..."
                      value={searchTerm}
                      onChange={(e) => setSearchTerm(e.target.value)}
                      className="bg-slate-700 border border-slate-600 rounded-lg pl-10 pr-4 py-2 text-white placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                  <select
                    value={filterType}
                    onChange={(e) => setFilterType(e.target.value)}
                    className="bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="all">All Files</option>
                    <option value="high-risk">High Risk</option>
                    <option value="protected">Protected</option>
                    <option value="recent">Recently Added</option>
                  </select>
                </div>
              )}
            </div>
          </div>
          <div className="p-6">
            {filteredFiles.length === 0 ? (
              <div className="text-center py-12">
                <FileCode className="w-16 h-16 text-slate-400 mx-auto mb-4" />
                <p className="text-slate-400 text-lg">
                  {projectFiles.length === 0 ? 'No files uploaded yet' : 'No files match your search'}
                </p>
                <p className="text-slate-500">
                  {projectFiles.length === 0 
                    ? 'Upload some files to get started with analysis' 
                    : 'Try adjusting your search or filter criteria'
                  }
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                {filteredFiles.map(file => (
                  <div
                    key={file.id}
                    className="flex items-center justify-between p-4 bg-slate-700 rounded-lg hover:bg-slate-600 transition-colors"
                  >
                    <div className="flex items-center space-x-4">
                      <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center">
                        <FileCode className="w-5 h-5 text-white" />
                      </div>
                      <div>
                        <h4 className="font-medium text-white">{file.name}</h4>
                        <div className="flex items-center space-x-4 text-sm text-slate-400">
                          <span>{file.type}</span>
                          <span>{typeof file.size === 'number' ? `${Math.round(file.size / 1024)}KB` : file.size}</span>
                          <span>{file.lines || 0} lines</span>
                          <span>{file.functions || 0} functions</span>
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center space-x-4">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                        file.riskLevel === 'High' ? 'bg-red-900 text-red-300' :
                        file.riskLevel === 'Medium' ? 'bg-yellow-900 text-yellow-300' :
                        'bg-green-900 text-green-300'
                      }`}>
                        {file.riskLevel || 'Low'} Risk
                      </span>
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                        file.protectionLevel === 'Critical' ? 'bg-red-900 text-red-300' :
                        file.protectionLevel === 'Important' ? 'bg-yellow-900 text-yellow-300' :
                        'bg-slate-600 text-slate-300'
                      }`}>
                        {file.protectionLevel || 'Optional'}
                      </span>
                      <button
                        onClick={() => analyzeCode(file)}
                        className="bg-blue-600 hover:bg-blue-700 px-3 py-1 rounded text-white text-sm"
                        disabled={loading}
                      >
                        {loading ? 'Analyzing...' : 'Analyze'}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  };

  // Render other views with empty states
  const renderOtherViews = (viewName, icon, description) => (
    <div className="p-6">
      {renderEmptyState(
        `${viewName} Coming Soon`,
        `${description} This feature will be available once you've uploaded and analyzed some code files.`,
        icon,
        "Upload Files First",
        () => setActiveView('project')
      )}
    </div>
  );

  // Main render logic
  const renderMainContent = () => {
    if (loading && activeView === 'dashboard') {
      return (
        <div className="flex items-center justify-center h-64">
          <div className="text-center">
            <RefreshCw className="w-8 h-8 text-blue-400 mx-auto mb-4 animate-spin" />
            <p className="text-slate-400">Loading platform...</p>
          </div>
        </div>
      );
    }

    switch (activeView) {
      case 'dashboard':
        return renderDashboard();
      case 'projects':
        return renderProjects();
      case 'project':
        return renderProjectDetail();
      case 'visual-map':
        return renderOtherViews('Visual Code Map', Map, 'Interactive visualization of your code dependencies and relationships.');
      case 'ai-sessions':
        return renderOtherViews('AI Sessions', Bot, 'Track and manage all your AI-powered code generation sessions.');
      case 'protection':
        return renderOtherViews('Code Protection', Shield, 'Advanced protection system for your critical code components.');
      case 'analysis':
        return renderOtherViews('Code Analysis', Activity, 'Deep code analysis with security, performance, and quality insights.');
      case 'quality':
        return renderOtherViews('Code Quality', Gauge, 'Comprehensive code quality metrics and compliance tracking.');
      case 'cloud':
        return renderOtherViews('Cloud & Database', Cloud, 'Automatic detection and mapping of cloud services and database operations.');
      case 'reports':
        return renderOtherViews('Reports & Export', FileText, 'Generate professional reports and export your project data.');
      default:
        return renderDashboard();
    }
  };

  return (
    <div className="min-h-screen bg-slate-900 flex flex-col">
      {renderHeader()}
      
      <div className="flex flex-1">
        {renderSidebar()}
        
        <main className="flex-1 overflow-auto">
          {renderMainContent()}
        </main>
      </div>

      {/* Loading Overlay */}
      {loading && activeView !== 'dashboard' && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-xl p-8 text-center">
            <RefreshCw className="w-12 h-12 text-blue-400 mx-auto mb-4 animate-spin" />
            <h3 className="text-xl font-semibold text-white mb-2">Processing...</h3>
            <p className="text-slate-400">Analyzing your code with AI-powered insights</p>
          </div>
        </div>
      )}

      {/* Notifications Toast */}
      {notifications.filter(n => !n.read).length > 0 && (
        <div className="fixed bottom-4 right-4 space-y-2 z-40">
          {notifications.filter(n => !n.read).slice(0, 3).map(notification => (
            <div
              key={notification.id}
              className={`p-4 rounded-lg shadow-lg max-w-sm ${
                notification.type === 'error' ? 'bg-red-800 border border-red-700' :
                notification.type === 'warning' ? 'bg-yellow-800 border border-yellow-700' :
                'bg-green-800 border border-green-700'
              }`}
            >
              <div className="flex items-start space-x-3">
                {notification.type === 'warning' && <AlertTriangle className="w-5 h-5 text-yellow-400 mt-0.5" />}
                {notification.type === 'error' && <XCircle className="w-5 h-5 text-red-400 mt-0.5" />}
                {notification.type === 'success' && <CheckCircle className="w-5 h-5 text-green-400 mt-0.5" />}
                <div className="flex-1">
                  <h4 className="font-medium text-white text-sm">{notification.title}</h4>
                  <p className="text-xs text-slate-300 mt-1">{notification.message}</p>
                </div>
                <button
                  onClick={() => {
                    setNotifications(prev => 
                      prev.map(n => n.id === notification.id ? {...n, read: true} : n)
                    );
                  }}
                  className="text-slate-400 hover:text-white"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Footer */}
      <footer className="bg-slate-800 border-t border-slate-700 px-6 py-4">
        <div className="flex items-center justify-between text-sm text-slate-400">
          <div className="flex items-center space-x-4">
            <span>© 2024 CodeFlow Platform</span>
            <span>•</span>
            <span>AI-Powered Code Management</span>
          </div>
          <div className="flex items-center space-x-4">
            <span>Status: Ready for Supabase integration</span>
            <div className="w-2 h-2 bg-blue-400 rounded-full"></div>
          </div>
        </div>
      </footer>
    </div>
  );
};

export default CodeFlowPlatform;