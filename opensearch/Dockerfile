FROM opensearchproject/opensearch:2.19.1

RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-kuromoji \
    && /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu
