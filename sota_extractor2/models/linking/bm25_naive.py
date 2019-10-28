import re
from decimal import Decimal
from dataclasses import dataclass
import numpy as np
import pandas as pd
from elasticsearch import Elasticsearch, client
import logging
#from .extractors import DatasetExtractor
import spacy
from scispacy.abbreviation import AbbreviationDetector
from sota_extractor2.models.linking.format import extract_value


@dataclass()
class Value:
    type: str
    value: str
    def __str__(self):
        return self.value


@dataclass()
class Cell:
    cell_ext_id: str
    table_ext_id: str
    row: int
    col: int


@dataclass()
class Proposal:
    cell: Cell
    dataset_values: list
    table_description: str
    model_values: list  # best paper competing
    model_params: dict = None
    raw_value: str = ""

    def __post_init__(self):
        if self.model_params is None:
            self.model_params = {}

    @property
    def dataset(self):
        return ' '.join(map(str, self.dataset_values)).strip()

    @property
    def model_name(self):
        return ' '.join(map(str, self.model_values)).strip()

    @property
    def model_type(self):
        types = [v.type for v in self.model_values] + ['']
        if 'model-competing' in types:
            return 'model-competing' # competing model is different from model-paper and model-best so we return it first
        return types[0]

    def __str__(self):
        return f"{self.model_name}: {self.raw_value} on {self.dataset}"

def mkquery_ngrams(query):
    return {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["dataset^3", "dataset.ngrams^1", "metric^1", "metric.ngrams^1", "task^1",
                               "task.ngrams^1"]
         }
      }
    }


def mkquery_fullmatch(query):
    return {
        "query": {
            "multi_match": {
                "query": query,
                "fields": ["dataset^3", "metric^1", "task^1"]
            }
        }
    }

class MatchSearch:
    def __init__(self, mkquery=mkquery_ngrams, es=None):
        self.case = True
        self.all_fields = True
        self.es = es or Elasticsearch()
        self.log = logging.getLogger(__name__)
        self.mkquery = mkquery

        self.nlp = spacy.load("en_core_web_sm")
        abbreviation_pipe = AbbreviationDetector(self.nlp)
        self.nlp.add_pipe(abbreviation_pipe)
        self.nlp.disable_pipes("tagger", "ner", "parser")

    def match_abrv(self, dataset, datasets):
        abrvs = []
        for ds in datasets:
            # "!" is a workaround to scispacy error
            doc = self.nlp(f"! {ds} ({dataset})")
            for abrv in doc._.abbreviations:
                if str(abrv) == dataset and str(abrv._.long_form) == ds:
                    abrvs.append(str(abrv._.long_form))
        abrvs = list(set(abrvs))
        if len(abrvs) == 1:
            print(f"abrv. for {dataset}: {abrvs[0]}")
            return abrvs[0]
        elif len(abrvs) == 0:
            return None
        else:
            print(f"Multiple abrvs. for {dataset}: {abrvs}")
            return None

    def preproc(self, val, datasets=None):
        val = val.strip(',- ')
        val = re.sub("dataset", '', val, flags=re.I)
        if datasets:
            abrv = self.match_abrv(val, datasets)
            if abrv:
                val += " " + abrv
        #         if self.case:
        #             val += (" " +re.sub("([a-z])([A-Z])", r'\1 \2', val)
        #                     +" " +re.sub("([a-zA-Z])([0-9])", r'\1 \2', val)
        #                    )
        return val

    def search(self, query, explain_doc_id=None):
        body = self.mkquery(query)
        if explain_doc_id is not None:
            return self.es.explain('et_taxonomy', doc_type='doc', id=explain_doc_id, body=body)
        return self.es.search('et_taxonomy', doc_type='doc', body=body)["hits"]

    def __call__(self, query, datasets, caption):
        split_re = re.compile('([^a-zA-Z0-9])')
        query = self.preproc(query, datasets).strip()
        if caption:
            query += " " + self.preproc(caption).strip()[:400]
        results = self.search(query)
        hits = results["hits"][:3]
        df = pd.DataFrame.from_records([
            dict(**hit["_source"],
                 confidence=hit["_score"] / len(split_re.split(query)),
                 # Roughly normalize the score not to ignore query length
                 evidence=query) for hit in hits
        ], columns=["dataset", "metric", "task", "confidence", "evidence"])
        if not len(df):
            self.log.debug("Elastic query didn't produce any output", query, hits)
        else:
            scores = []
            for dataset in df["dataset"]:
                r = self.search(dataset)
                scores.append(
                    dict(ok_score=r['hits'][0]['_score'] / len(split_re.split(dataset)),
                         bad_score=r['hits'][1]['_score'] / len(split_re.split(dataset))))

            scores = pd.DataFrame.from_records(scores)
            df['confidence'] = ((scores['ok_score'] - scores['bad_score']) / scores['bad_score']) * df['confidence'] / scores['ok_score']
        return df[["dataset", "metric", "task", "confidence", "evidence"]]

float_pm_re = re.compile(r"(±?)([+-]?\s*(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?)\s*(%?)")
whitespace_re = re.compile(r"\s+")
def handle_pm(value):
    "handle precentage metric"
    for match in float_pm_re.findall(value):
        if not match[0]:
            try:
                yield Decimal(whitespace_re.sub("", match[1])) / (100 if match[-1] else 1)
            except:
                pass
            # %%


proposal_columns = ['dataset', 'metric', 'task', 'format', 'raw_value', 'model', 'model_type', 'cell_ext_id',
                    'confidence', 'parsed', 'struct_model_type', 'struct_dataset']


def generate_proposals_for_table(table_ext_id,  matrix, structure, desc, taxonomy_linking, datasets):
    # %%
    # Proposal generation
    def consume_cells(matrix):
        for row_id, row in enumerate(matrix):
            for col_id, cell in enumerate(row):
                yield (row_id, col_id, cell)


    def annotations(r, c, type='model'):
        for nc in range(0, c):
            if type in structure[r, nc]:
                yield Value(structure[r, nc], matrix[r, nc])
        for nr in range(0, r):
            if type in structure[nr, c]:
                yield Value(structure[nr, c], matrix[nr, c])


    number_re = re.compile(r'^[± Ee /()^0-9.%±_-]{2,}$')

    proposals = [Proposal(
        cell=Cell(cell_ext_id=f"{table_ext_id}/{r}.{c}",
                  table_ext_id=table_ext_id,
                  row=r,
                  col=c
                  ),
        # TODO Add table type: sota / error ablation
        table_description=desc,
        model_values=list(annotations(r, c, 'model')),
        dataset_values=list(annotations(r, c, 'dataset')),
        raw_value=val)
        for r, c, val in consume_cells(matrix)
        if structure[r, c] == '' and number_re.match(matrix[r, c].strip())]

    # def empty_proposal(cell_ext_id, reason):
    #     np = "not-present"
    #     return dict(
    #         dataset=np, metric=np, task=np, format=np, raw_value=np, model=np,
    #         model_type=np, cell_ext_id=cell_ext_id, confidence=-1, debug_reason=reason
    #     )

    def linked_proposals(proposals):
        for prop in proposals:
            df = taxonomy_linking(prop.dataset, datasets, desc, debug_info=prop)
            assert len(df) == 1

            metric = df['metric'][0]

            # heuristyic to handle accuracy vs error
            first_num = (list(handle_pm(prop.raw_value)) + [0])[0]
            format = "{x}"
            # if first_num > 1:
            #     first_num /= 100
            #     format = "{x/100}"
            if first_num < 1 and '%' not in prop.raw_value:
                first_num *= 100
                format = "{100*x}"
            if '%' in prop.raw_value:
                format += '%'

            # if ("error" in metric or "Error" in metric) and (first_num > 0.5):
            if (metric.strip().lower() == "error") and (first_num > 0.5):
                metric = "Accuracy"

            linked = {
                'dataset': df['dataset'][0],
                'metric': metric,
                'task': df['task'][0],
                'format': format,
                'raw_value': prop.raw_value,
                'model': prop.model_name,
                'model_type': prop.model_type,
                'cell_ext_id': prop.cell.cell_ext_id,
                'confidence': df['confidence'][0],
                'struct_model_type': prop.model_type,
                'struct_dataset': prop.dataset
            }
            yield linked

    # specify columns in case there's no proposal

    proposals = pd.DataFrame.from_records(list(linked_proposals(proposals)), columns=proposal_columns)

    if len(proposals):
        proposals["parsed"]=proposals[["raw_value", "format"]].apply(
            lambda row: float(extract_value(row.raw_value, row.format)), axis=1)
    return proposals


def linked_proposals(paper_ext_id, paper, annotated_tables, taxonomy_linking=MatchSearch(),
                     dataset_extractor=None):
    #                     dataset_extractor=DatasetExtractor()):
    proposals = []
    datasets = dataset_extractor.from_paper(paper)
    #print(f"Extracted datasets: {datasets}")
    for idx, table in enumerate(annotated_tables):
        matrix = np.array(table.matrix)
        structure = np.array(table.matrix_tags)
        tags = 'sota'
        desc = table.caption
        table_ext_id = f"{paper_ext_id}/{table.name}"

        if 'sota' in tags and 'no_sota_records' not in tags: # only parse tables that are marked as sota
            proposals.append(generate_proposals_for_table(table_ext_id, matrix, structure, desc, taxonomy_linking, datasets))
    if len(proposals):
        return pd.concat(proposals)
    return pd.DataFrame(columns=proposal_columns)


def test_link_taxonomy():
    link_taxonomy_raw = MatchSearch()
    results = link_taxonomy_raw.search(link_taxonomy_raw.preproc("miniImageNet 5-way 1-shot"))
    # assert "Mini-ImageNet - 1-Shot Learning" == results["hits"][0]["_source"]["dataset"], results
    results = link_taxonomy_raw.search(link_taxonomy_raw.preproc("CoNLL2003"))
    assert "CoNLL 2003 (English)" == results["hits"][0]["_source"]["dataset"], results
    results = link_taxonomy_raw.search(link_taxonomy_raw.preproc("AGNews"))
    assert "AG News" == results["hits"][0]["_source"]["dataset"], results
    link_taxonomy_raw("miniImageNet 5-way 1-shot")
    # %%
    split_re = re.compile('([^a-zA-Z0-9])')

    # %%
    q = "miniImageNet 5-way 1-shot Mini ImageNet 1-Shot Learning" * 1
    r = link_taxonomy_raw.search(q)
    f = len(split_re.split(q))
    r['hits'][0]['_score'] / f, r['hits'][1]['_score'] / f, r['hits'][0]['_source']
    # %%
    q = "Mini ImageNet 1-Shot Learning" * 1
    r = link_taxonomy_raw.search(q)
    f = len(split_re.split(q))
    r['hits'][0]['_score'] / f, r['hits'][1]['_score'] / f, r['hits'][0]['_source']
    # %%
    q = "Mini ImageNet 1-Shot" * 1
    r = link_taxonomy_raw.search(q)
    f = len(split_re.split(q))
    r['hits'][0]['_score'] / f, r['hits'][1]['_score'] / f, r['hits'][0]['_source']
    #
    # # %%
    # prop = proposals[1]
    # print(prop)
    # # todo issue with STS-B matching IJB-B
    # link_taxonomy_raw(prop.dataset)

